"""irc3 plugin — handles IRC events and drives DCC downloads."""

import asyncio
import logging
import re
from pathlib import Path

import irc3

from config import IrcConfig, SearchConfig
from dcc import (
    DccOffer,
    DownloadError,
    FileTooLargeError,
    InvalidOfferError,
    extract_ebook,
    parse_dcc_offer,
    receive_dcc,
    safe_download_path,
)

log = logging.getLogger(__name__)

# Defaults; EbooksAgent overrides them per instance from the loaded config
CHANNEL = IrcConfig().channel
OFFER_TIMEOUT = SearchConfig().offer_timeout

# A request like "!SomeBot Author - Title.epub" names the bot that should
# serve the file. When present, only that bot's offer is accepted.
_BOT_REQUEST_RE = re.compile(r"^!(?P<nick>\S+)")


@irc3.plugin
class EbooksPlugin:
    def __init__(self, bot):
        self.bot = bot
        self.channel = CHANNEL
        self.offer_timeout = OFFER_TIMEOUT
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

    # **kwargs - pick up and discard any additional arguments
    # to prevent error from being thrown
    def on_connected(self, **kwargs):
        # Once connected, join the #ebooks channel
        self.bot.join(self.channel)
        # Signal to the parent that we're done with the connected lifecycle
        self._connected.set()
        print(f"Connected to {self.bot.config.host} and joined {self.channel}")

    def _is_channel_member(self, nick: str) -> bool:
        # irc3's userlist plugin tracks who's in each channel we've joined.
        # .get() so we don't create an empty entry in its defaultdict
        members = self.bot.channels.get(self.channel, ())
        return any(m.lower() == nick.lower() for m in members)

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
            offer = parse_dcc_offer(text)
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

    async def search(self, term: str) -> Path:
        async with self._search_lock:
            m = _BOT_REQUEST_RE.match(term)
            self._expected_sender = m.group("nick") if m else None
            self._pending = asyncio.get_running_loop().create_future()
            try:
                # Send our message to the server
                self.bot.privmsg(self.channel, term)
                log.info("Message sent: %s", term)

                # Wait for a validated offer addressed to us
                try:
                    offer = await asyncio.wait_for(self._pending, self.offer_timeout)
                except asyncio.TimeoutError:
                    raise TimeoutError(
                        f"No DCC offer received within {self.offer_timeout:g}s for '{term}'"
                    ) from None
            finally:
                # Close the slot immediately — any further offers (late,
                # duplicate or spoofed) are dropped as unsolicited
                self._pending = None
                self._expected_sender = None

            # The filename is chosen by the remote bot — never trust it as a path.
            # Raises UnsafeFilenameError if it can't be made safe.
            dest = safe_download_path(offer.filename)
            log.info(
                "DCC SEND offer: %s from %s:%s (%s bytes)",
                offer.filename, offer.ip, offer.port, offer.filesize,
            )
            # If we've made it this far, we have a vetted offer
            # Lets receive it, and extract if its a zip
            # May come back as e.g. "Dune (1).epub" if the name was taken
            dest = await receive_dcc(offer.ip, offer.port, offer.filesize, dest)
            log.info("Download complete: %s", dest)
            if dest.suffix.lower() == ".zip":
                dest = extract_ebook(dest, term)
                log.info("Extracted: %s", dest)
            return dest
