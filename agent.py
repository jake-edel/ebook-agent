"""EbooksAgent — owns the IRC connection lifecycle."""

import asyncio
import hashlib
import logging
from pathlib import Path

import irc3

from plugin import EbooksPlugin

log = logging.getLogger(__name__)

HOST = "irc.irchighway.net"
# TLS port. IRCHighway round-robins across several servers, each with its own
# self-signed certificate, and doesn't publish the list — so certificates
# can't be verified or pinned. We still use TLS: it keeps searches and nick
# private from passive observers (shared Wi-Fi, ISP). It does NOT stop an
# active man-in-the-middle; note DCC file transfers are plaintext regardless.
PORT = 6697


class EbooksAgent:
    def __init__(self, nick: str = "pt_bookbot"):
        self._nick = nick
        self._bot: irc3.IrcBot | None = None
        self._plugin: EbooksPlugin | None = None

    def _build_bot(self) -> irc3.IrcBot:
        # Configure our server / user settings
        bot = irc3.IrcBot.from_config({
            "nick": self._nick,
            "autojoins": [],
            "host": HOST,
            "port": PORT,
            "ssl": True,
            # Encrypt without verifying — see the note on PORT above
            "ssl_verify": "CERT_NONE",
            "includes": [
                "irc3.plugins.core",
                "irc3.plugins.userlist",
            ],
        })
        # Register our plugin
        bot.include(EbooksPlugin)
        # Get back our plugin instance,
        # now tied to irc3 lifecycle
        plugin = bot.get_plugin(EbooksPlugin)
        # Add our event handlers
        # 1. Listening for a connection
        # 2. Listening for a DCC file send
        bot.attach_events(
            irc3.event(irc3.rfc.CONNECTED, plugin.on_connected),
            irc3.event(
                # Mirrors irc3's own PRIVMSG pattern: optional IRCv3 tags, then the
                # ":" prefix marker *outside* the mask group so the nick parses as
                # "Search", not ":Search"
                r"^(@(?P<tags>\S+) )?:(?P<mask>\S+!\S+@\S+) (?:PRIVMSG|NOTICE) (?P<target>\S+) :(?P<text>.*DCC SEND.*)",
                plugin.on_dcc_send,
            ),
        )
        # Return a reference to our bot
        return bot

    async def start(self):
        # Create our bot and wait before we do anything else
        self._bot = self._build_bot()
        self._plugin = self._bot.get_plugin(EbooksPlugin)
        self._bot.create_connection()
        try:
            async with asyncio.timeout(30):
                await self._plugin._connected.wait()
        except TimeoutError:
            raise TimeoutError(f"Could not connect to {HOST}:{PORT} within 30s") from None
        self._log_tls()

    def _log_tls(self):
        # Record which server we landed on and its certificate fingerprint,
        # so a change is at least visible in the logs
        transport = self._bot.protocol.transport
        tls = transport.get_extra_info("ssl_object")
        peer = transport.get_extra_info("peername")
        der = tls.getpeercert(binary_form=True) if tls else None
        fp = hashlib.sha256(der).hexdigest()[:16] if der else "unknown"
        log.info("TLS %s to %s (cert sha256 %s…, unverified)", tls.version() if tls else "?", peer[0], fp)

    def stop(self):
        if self._bot:
            self._bot.quit("bye")

    async def search(self, term: str) -> Path:
        if self._plugin is None:
            raise RuntimeError("Not connected — call start() first")
        # No catch-all timeout here: each stage has its own limit instead.
        # The plugin waits 60s for an offer (counted from when *this* search
        # gets its turn, not while queued behind another), and the transfer
        # has connect and stall timeouts — so a slow but healthy download
        # isn't cut off partway through.
        return await self._plugin.search(term)
