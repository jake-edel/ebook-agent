"""irc3 plugin — handles IRC events and drives DCC downloads."""

import asyncio
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import irc3

from config import Config
from core import ConnectionState, RequestState
from dcc import DccOffer, DownloadError, Downloads, FileTooLargeError, InvalidOfferError

log = logging.getLogger(__name__)

# A request like "!SomeBot Author - Title.epub" names the bot that should
# serve the file. When present, only that bot's offer is accepted.
_BOT_REQUEST_RE = re.compile(r"^!(?P<nick>\S+)\s+\S")


@dataclass
class _Waiter:
    """A line we've sent that's waiting for its DCC offer."""
    # The bot that must answer, or None for a search (any channel member)
    sender: str | None
    offer: asyncio.Future[DccOffer]
    # The caller's name for this request (EbooksAgent: the request id), so
    # bot messages can be linked to it
    key: object = None


def _ignore(**changes):
    pass


@irc3.plugin
class EbooksPlugin:
    def __init__(self, bot):
        self.bot = bot
        # irc3 creates the plugin, so EbooksAgent passes the loaded config
        # through the bot's config
        config: Config = bot.config["ebooks_config"]
        self.host = config.irc.host
        self.channel = config.irc.channel
        self.offer_timeout = config.downloads.offer_timeout
        self.show_private_messages = config.irc.show_private_messages
        self.downloads = Downloads(config.downloads)
        # Set once we're in the channel (end of NAMES), cleared on disconnect
        self.ready = asyncio.Event()
        # Set when the server connection drops (e.g. after our QUIT)
        self.disconnected = asyncio.Event()
        # Called with the new state as we connect, join and drop
        self.on_status: Callable[[ConnectionState], None] = lambda state: None
        # Called when someone may have joined or left the channel
        self.on_members_changed: Callable[[], None] = lambda: None
        # Called with (nick, text, key) for each message a user sends us
        # directly. `key` is that of the request the message most likely
        # belongs to (see _match), or None.
        self.on_bot_message: Callable[[str, str, object], None] = lambda nick, text, key: None
        # Lines sent and waiting for an offer, oldest first. Offers that match
        # none of them are dropped as unsolicited.
        self._waiters: list[_Waiter] = []
        # Any channel member may answer a search, so two at once would be
        # ambiguous: searches go one at a time. Book requests name their bot
        # and don't wait for each other.
        self._search_lock = asyncio.Lock()

    def connection_lost(self):
        # irc3 calls this on every plugin when the connection drops
        self.disconnected.set()
        self.ready.clear()
        # Our nick is gone, and with it any place in a bot's queue: fail the
        # lines still waiting for an offer rather than resend them (a bot that
        # kept the spot would serve us twice). Transfers carry on; DCC is a
        # separate connection. Lines not sent yet wait for the reconnect.
        for w in self._waiters:
            if not w.offer.done():
                w.offer.set_exception(ConnectionError("Disconnected from IRC before an offer arrived"))
        # EbooksAgent.stop() marks the connection closed before its QUIT
        if getattr(getattr(self.bot, "protocol", None), "closed", False):
            log.info("Connection to %s closed", self.host)
        else:
            log.warning("Disconnected from %s; reconnecting", self.host)
        self.on_status(ConnectionState.DISCONNECTED)
        self.on_members_changed()

    # **kwargs - pick up and discard any additional arguments
    # to prevent error from being thrown
    def on_connected(self, **kwargs):
        self.disconnected.clear()
        # Once connected, join the #ebooks channel. We're ready once the
        # server has sent the member list (on_names_end).
        self.bot.join(self.channel)
        log.info("Connected to %s, joining %s", self.host, self.channel)
        self.on_status(ConnectionState.CONNECTING)

    def on_names_end(self, channel=None, **kwargs):
        # End of NAMES (366) for our channel: we're in, and the member list the
        # sender check relies on is filled in
        if channel is None or channel.lower() != self.channel.lower() or self.ready.is_set():
            return
        self.ready.set()
        log.info("Joined %s", self.channel)
        self.on_status(ConnectionState.READY)
        self.on_members_changed()

    def on_membership(self, **kwargs):
        # A JOIN, PART, QUIT, KICK or NICK. irc3's userlist plugin keeps the
        # member list itself; we only pass on that it may have changed.
        self.on_members_changed()

    def members(self) -> frozenset[str]:
        """Who's in the channel, lower-cased. Empty until we've joined."""
        if not self.ready.is_set():
            return frozenset()
        # irc3's userlist plugin tracks who's in each channel we've joined.
        # .get() so we don't create an empty entry in its defaultdict
        return frozenset(m.lower() for m in self.bot.channels.get(self.channel, ()))

    def _is_channel_member(self, nick: str) -> bool:
        return nick.lower() in self.members()

    def on_message(self, mask=None, command=None, target=None, text=None, **kwargs):
        # Log everything addressed to us (DMs, bot replies, DCC offers, server
        # notices) verbatim. Channel chatter is skipped — it would drown the log.
        if target and target[0] in "#&+!":
            return
        if self.show_private_messages:
            log.info("%s from %s to %s: %r", command, mask or "server", target, text)
        # Pass on what users (not the server) tell us, except CTCP like DCC offers
        nick = getattr(mask, "nick", None)
        if nick and text and not text.startswith("\x01"):
            waiter = self._match(nick)
            self.on_bot_message(nick, text, waiter.key if waiter else None)

    def on_dcc_send(self, mask=None, target=None, text=None, **kwargs):
        sender = getattr(mask, "nick", None) or str(mask).split("!", 1)[0]

        # DCC offers are always addressed to us directly. Anything sent to the
        # channel (or anywhere else) is just someone typing "DCC SEND".
        if target is None or target.lower() != self.bot.nick.lower():
            return

        # Parse up front, but hold on to any validation error until we know
        # whether this offer came from someone we're actually waiting on
        error: DownloadError | None = None
        try:
            offer = self.downloads.parse_offer(text)
        except (InvalidOfferError, FileTooLargeError) as e:
            offer, error = None, e
        if offer is None and error is None:
            return  # not a DCC SEND at all

        # The sender must be someone in #ebooks
        if not self._is_channel_member(sender):
            log.warning("Ignoring DCC offer from %s — not in %s", sender, self.channel)
            return

        # ...and someone we're waiting on: the oldest request naming that bot
        # (bots serve requests in order), otherwise the search in flight
        waiter = self._match(sender)
        if waiter is None:
            log.warning(
                "Ignoring unsolicited DCC offer from %s: %s",
                sender, offer.filename if offer else error,
            )
            return

        if error is not None:
            # The bot we asked replied, but with something we won't accept
            # (too large, bad address...). Fail the request now with the real
            # reason instead of letting it sit until the timeout.
            log.warning("Rejected DCC offer from %s: %s", sender, error)
            waiter.offer.set_exception(error)
            return

        waiter.offer.set_result(offer)

    def _match(self, sender: str) -> _Waiter | None:
        """Who an offer or message from `sender` is for: the oldest request
        naming that bot (bots serve requests in order), else the search in
        flight, which any channel member may answer."""
        open_ = [w for w in self._waiters if not w.offer.done()]
        for w in open_:
            if w.sender and w.sender.lower() == sender.lower():
                return w
        return next((w for w in open_ if w.sender is None), None)

    async def search(self, query: str, update=_ignore, key=None) -> Path:
        """Send `@search <query>` and save the results file to the searches dir.

        The file is named "<timestamp> <query>.txt", without the "@search "
        prefix, so past searches sort by date. `query` must have passed
        check_query.

        `update(**changes)` is called with Request fields as the work
        progresses (state, filename, size, received). `key` is passed back
        with bot messages that belong to this search.
        """
        async with self._search_lock:
            offer = await self._fetch(f"@search {query}", None, update, key)
        # Results arrive zipped ("SearchBot_results_for_ dune.txt.zip") or,
        # occasionally, as a bare .txt. Either way they end up as
        # "<timestamp> <query>.txt" in the searches dir.
        name = f"{time.strftime('%Y%m%d-%H%M%S')} {_filename_part(query)}"
        dl = self.downloads
        if _is_zip(offer.filename):
            path = await self._receive(offer, dl.safe_path(offer.filename, dl.searches_dir), update)
            path = await asyncio.to_thread(dl.extract, path, name)
            log.info("Extracted: %s", path)
        else:
            suffix = Path(offer.filename).suffix.lower() or ".txt"
            path = await self._receive(offer, dl.safe_path(f"{name}{suffix}", dl.searches_dir), update)
        return path

    async def request(self, line: str, bot: str, update=_ignore, key=None) -> Path:
        """Send a `!Bot <file>` line and save the book to the downloads dir.

        `line` and `bot` come from check_request. Only an offer from that bot
        is accepted. The book keeps the name the bot gave it (sanitized), or
        its name inside the zip if it came zipped.
        """
        offer = await self._fetch(line, bot, update, key)
        path = await self._receive(offer, self.downloads.safe_path(offer.filename), update)
        if _is_zip(offer.filename):
            path = await asyncio.to_thread(self.downloads.extract, path)
            log.info("Extracted: %s", path)
        return path

    async def _fetch(self, line: str, sender: str | None, update, key) -> DccOffer:
        """Send `line` to the channel and wait for a vetted offer from `sender`."""
        # Not connected (yet, or any more)? Wait for the reconnect
        await self.ready.wait()
        waiter = _Waiter(sender, asyncio.get_running_loop().create_future(), key)
        self._waiters.append(waiter)
        try:
            # Send our message to the server
            self.bot.privmsg(self.channel, line)
            log.info("Message sent: %s", line)
            update(state=RequestState.WAITING)

            # Wait for a validated offer addressed to us
            try:
                return await asyncio.wait_for(waiter.offer, self.offer_timeout)
            except asyncio.TimeoutError:
                raise TimeoutError(
                    f"No DCC offer received within {self.offer_timeout:g}s for '{line}'"
                ) from None
        finally:
            # Stop waiting immediately — any further offers (late, duplicate
            # or spoofed) are dropped as unsolicited
            self._waiters.remove(waiter)

    async def _receive(self, offer: DccOffer, dest: Path, update) -> Path:
        """Download a vetted offer to `dest` (built with safe_path).

        May come back as e.g. "Dune (1).epub" if the name was taken.
        """
        log.info(
            "DCC SEND offer: %s from %s:%s (%s bytes)",
            offer.filename, offer.ip, offer.port, offer.filesize,
        )
        update(state=RequestState.TRANSFERRING, filename=offer.filename, size=offer.filesize)
        dest = await self.downloads.receive(
            offer.ip, offer.port, offer.filesize, dest,
            on_progress=lambda received: update(received=received),
        )
        log.info("Download complete: %s", dest)
        return dest


def check_query(query: str) -> str:
    """Validate a search query; returns it stripped. Raises ValueError."""
    query = _check_line(query, "Search query").strip()
    if not query:
        raise ValueError("Search query is empty")
    _check_length(f"@search {query}", "Search query")
    return query


def check_request(line: str) -> tuple[str, str]:
    """Validate a `!Bot <file>` line; returns (line, bot). Raises ValueError."""
    line = _check_line(line, "Request").strip()
    m = _BOT_REQUEST_RE.match(line)
    if not m:
        raise ValueError(f"Not a book request (expected '!<bot> <file>'): {line!r}")
    _check_length(line, "Request")
    return line, m.group("nick")


# The server relays our line to the bots as
# ":<nick>!<user>@<host> PRIVMSG #ebooks :<line>", and all of that must fit in
# IRC's 512 bytes, or the end is cut off. irc3 doesn't refuse long lines: it
# splits them into several messages. 350 leaves room for a long nick and host;
# real result lines are under 200.
MAX_LINE_BYTES = 350


def _check_line(text: str, what: str) -> str:
    """Refuse text that would smuggle extra IRC commands onto the wire."""
    if any(c in text for c in "\r\n\x00"):
        raise ValueError(f"{what} can't contain line breaks or NUL characters")
    return text


def _check_length(line: str, what: str):
    size = len(line.encode())
    if size > MAX_LINE_BYTES:
        raise ValueError(f"{what} is too long for IRC ({size} bytes, at most {MAX_LINE_BYTES})")


def _filename_part(query: str) -> str:
    """Make a query safe to embed in a filename.

    safe_path keeps only the last path component, so "AC/DC" would
    become "DC". Replace separators first so the whole query survives.
    """
    return re.sub(r"[/\\]", "_", query)


def _is_zip(filename: str) -> bool:
    return filename.lower().endswith(".zip")
