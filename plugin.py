"""irc3 plugin — handles IRC events and drives DCC downloads."""

import asyncio
import logging
import re
import time
from pathlib import Path

import irc3

from config import Config
from dcc import DccOffer, DownloadError, Downloads, FileTooLargeError, InvalidOfferError

log = logging.getLogger(__name__)

# A request like "!SomeBot Author - Title.epub" names the bot that should
# serve the file. When present, only that bot's offer is accepted.
_BOT_REQUEST_RE = re.compile(r"^!(?P<nick>\S+)\s+\S")


@irc3.plugin
class EbooksPlugin:
    def __init__(self, bot):
        self.bot = bot
        # irc3 creates the plugin, so settings can't go through __init__;
        # EbooksAgent calls configure() with the loaded config right after
        self.configure(Config())
        self._connected = asyncio.Event()
        # Only one search runs at a time, and it owns the single pending slot
        # below. That way an offer can only ever satisfy the search that's
        # currently waiting — never a later one.
        self._search_lock = asyncio.Lock()
        # Future resolved by on_dcc_send with the accepted offer.
        # None whenever no search is waiting, so unsolicited offers are dropped.
        self._pending: asyncio.Future[DccOffer] | None = None
        # Nick the current search expects the offer from, if the term named one
        self._expected_sender: str | None = None

    def configure(self, config: Config):
        self.channel = config.irc.channel
        self.offer_timeout = config.search.offer_timeout
        self.downloads = Downloads(config.downloads)

    # **kwargs - pick up and discard any additional arguments
    # to prevent error from being thrown
    def on_connected(self, **kwargs):
        # Once connected, join the #ebooks channel
        self.bot.join(self.channel)
        # Signal to the parent that we're done with the connected lifecycle
        self._connected.set()
        log.info("Connected to %s and joined %s", self.bot.config.host, self.channel)

    def _is_channel_member(self, nick: str) -> bool:
        # irc3's userlist plugin tracks who's in each channel we've joined.
        # .get() so we don't create an empty entry in its defaultdict
        members = self.bot.channels.get(self.channel, ())
        return any(m.lower() == nick.lower() for m in members)

    def on_message(self, mask=None, command=None, target=None, text=None, **kwargs):
        # Log everything addressed to us (DMs, bot replies, DCC offers, server
        # notices) verbatim. Channel chatter is skipped — it would drown the log.
        if target and target[0] in "#&+!":
            return
        log.info("%s from %s to %s: %r", command, mask or "server", target, text)

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

        # Drop offers that arrive when we didn't ask for anything, or after
        # the current search already accepted one
        if self._pending is None or self._pending.done():
            log.warning(
                "Ignoring unsolicited DCC offer from %s: %s",
                sender, offer.filename if offer else error,
            )
            return

        # If the search named a bot ("!Bot ..."), only that bot may answer
        if self._expected_sender and sender.lower() != self._expected_sender.lower():
            log.warning(
                "Ignoring DCC offer from %s — expected %s", sender, self._expected_sender
            )
            return

        # Otherwise the sender must at least be someone in #ebooks
        if not self._is_channel_member(sender):
            log.warning("Ignoring DCC offer from %s — not in %s", sender, self.channel)
            return

        if error is not None:
            # The bot we asked replied, but with something we won't accept
            # (too large, bad address...). Fail the search now with the real
            # reason instead of letting it sit until the timeout.
            log.warning("Rejected DCC offer from %s: %s", sender, error)
            self._pending.set_exception(error)
            return

        self._pending.set_result(offer)

    async def search(self, query: str) -> Path:
        """Send `@search <query>` and save the results file to the searches dir.

        The file is named "<timestamp> <query>.txt", without the "@search "
        prefix, so past searches sort by date. Raises ValueError for an
        empty query or one containing line breaks.
        """
        query = _check_line(query, "Search query").strip()
        if not query:
            raise ValueError("Search query is empty")
        offer = await self._fetch(f"@search {query}", expected_sender=None)
        # Results arrive zipped ("SearchBot_results_for_ dune.txt.zip") or,
        # occasionally, as a bare .txt. Either way they end up as
        # "<timestamp> <query>.txt" in the searches dir.
        name = f"{time.strftime('%Y%m%d-%H%M%S')} {_filename_part(query)}"
        dl = self.downloads
        if _is_zip(offer.filename):
            path = await self._receive(offer, dl.safe_path(offer.filename, dl.searches_dir))
            path = dl.extract(path, name)
            log.info("Extracted: %s", path)
        else:
            suffix = Path(offer.filename).suffix.lower() or ".txt"
            path = await self._receive(offer, dl.safe_path(f"{name}{suffix}", dl.searches_dir))
        return path

    async def request(self, line: str) -> Path:
        """Send a `!Bot <file>` line and save the book to the downloads dir.

        Only an offer from that bot is accepted. The book keeps the name the
        bot gave it (sanitized), or its name inside the zip if it came zipped.
        Raises ValueError if `line` isn't of the form "!<bot> <something>".
        """
        line = _check_line(line, "Request").strip()
        m = _BOT_REQUEST_RE.match(line)
        if not m:
            raise ValueError(f"Not a book request (expected '!<bot> <file>'): {line!r}")
        offer = await self._fetch(line, expected_sender=m.group("nick"))
        path = await self._receive(offer, self.downloads.safe_path(offer.filename))
        if _is_zip(path.name):
            path = self.downloads.extract(path)
            log.info("Extracted: %s", path)
        return path

    async def _fetch(self, line: str, expected_sender: str | None) -> DccOffer:
        """Send `line` to the channel and wait for a vetted offer in reply."""
        async with self._search_lock:
            self._expected_sender = expected_sender
            self._pending = asyncio.get_running_loop().create_future()
            try:
                # Send our message to the server
                self.bot.privmsg(self.channel, line)
                log.info("Message sent: %s", line)

                # Wait for a validated offer addressed to us
                try:
                    return await asyncio.wait_for(self._pending, self.offer_timeout)
                except asyncio.TimeoutError:
                    raise TimeoutError(
                        f"No DCC offer received within {self.offer_timeout:g}s for '{line}'"
                    ) from None
            finally:
                # Close the slot immediately — any further offers (late,
                # duplicate or spoofed) are dropped as unsolicited
                self._pending = None
                self._expected_sender = None

    async def _receive(self, offer: DccOffer, dest: Path) -> Path:
        """Download a vetted offer to `dest` (built with safe_path).

        May come back as e.g. "Dune (1).epub" if the name was taken.
        """
        log.info(
            "DCC SEND offer: %s from %s:%s (%s bytes)",
            offer.filename, offer.ip, offer.port, offer.filesize,
        )
        dest = await self.downloads.receive(offer.ip, offer.port, offer.filesize, dest)
        log.info("Download complete: %s", dest)
        return dest


def _check_line(text: str, what: str) -> str:
    """Refuse text that would smuggle extra IRC commands onto the wire."""
    if any(c in text for c in "\r\n\x00"):
        raise ValueError(f"{what} can't contain line breaks or NUL characters")
    return text


def _filename_part(query: str) -> str:
    """Make a query safe to embed in a filename.

    safe_path keeps only the last path component, so "AC/DC" would
    become "DC". Replace separators first so the whole query survives.
    """
    return re.sub(r"[/\\]", "_", query)


def _is_zip(filename: str) -> bool:
    return filename.lower().endswith(".zip")
