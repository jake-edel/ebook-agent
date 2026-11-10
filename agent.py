"""EbooksAgent — owns the IRC connection lifecycle."""

import asyncio
import hashlib
import logging
from pathlib import Path

import irc3

from config import Config
from plugin import EbooksPlugin

log = logging.getLogger(__name__)

# How long stop() waits for the server to hang up after our QUIT
QUIT_TIMEOUT = 2.0

# TLS is always on and isn't configurable. IRCHighway round-robins across
# several servers, each with its own self-signed certificate, and doesn't
# publish the list — so certificates can't be verified or pinned. We still use
# TLS: it keeps searches and nick private from passive observers (shared
# Wi-Fi, ISP). It does NOT stop an active man-in-the-middle; note DCC file
# transfers are plaintext regardless.


class EbooksAgent:
    def __init__(self, config: Config | None = None):
        self._config = config or Config()
        self._bot: irc3.IrcBot | None = None
        self._plugin: EbooksPlugin | None = None

    def _build_bot(self) -> irc3.IrcBot:
        # Configure our server / user settings
        irc = self._config.irc
        bot = irc3.IrcBot.from_config({
            "nick": irc.nick,
            "autojoins": [],
            "host": irc.host,
            "port": irc.port,
            "ssl": True,
            # Encrypt without verifying — see the note at the top of this file
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
        plugin.configure(self._config)
        # Add our event handlers
        # 1. Listening for a connection
        # 2. Listening for the end of the channel's member list (we're in)
        # 3. Logging every message addressed to us
        # 4. Listening for a DCC file send
        bot.attach_events(
            irc3.event(irc3.rfc.CONNECTED, plugin.on_connected),
            irc3.event(irc3.rfc.RPL_ENDOFNAMES, plugin.on_names_end),
            irc3.event(
                # Any PRIVMSG/NOTICE. The prefix is optional and needn't be a
                # nick!user@host, so server notices are caught too.
                r"^(@(?P<tags>\S+) )?(:(?P<mask>\S+) )?(?P<command>PRIVMSG|NOTICE) (?P<target>\S+) :(?P<text>.*)",
                plugin.on_message,
            ),
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
        self._plugin.downloads.create_dirs()
        self._bot.create_connection()
        irc = self._config.irc
        try:
            async with asyncio.timeout(irc.connect_timeout):
                await self._plugin.ready.wait()
        except TimeoutError:
            raise TimeoutError(
                f"Could not connect to {irc.host}:{irc.port} within {irc.connect_timeout:g}s"
            ) from None
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

    async def stop(self, timeout: float = QUIT_TIMEOUT):
        """Send QUIT and wait (up to `timeout`) for the server to close the connection."""
        protocol = getattr(self._bot, "protocol", None)
        if protocol is None or protocol.closed:
            return  # never connected, or already gone
        # irc3 reconnects whenever a connection it didn't close drops. The
        # server is about to drop this one on purpose, so mark it closed first.
        protocol.closed = True
        try:
            async with asyncio.timeout(timeout):
                # send_line's future resolves once the line is written out
                await self._bot.send_line("QUIT :bye")
                await self._plugin.disconnected.wait()
            log.info("Disconnected from %s", self._config.irc.host)
        except TimeoutError:
            log.warning("Server didn't close the connection within %gs of QUIT", timeout)
        finally:
            protocol.transport.close()

    def _require_plugin(self) -> EbooksPlugin:
        if self._plugin is None:
            raise RuntimeError("Not connected — call start() first")
        return self._plugin

    # No catch-all timeout on either command: each stage has its own limit.
    # The plugin waits for an offer (counted from when *this* request gets
    # its turn, not while queued behind another), and the transfer has
    # connect and stall timeouts, so a slow but healthy download isn't cut
    # off partway through.

    async def search(self, query: str) -> Path:
        """`@search <query>`; returns the results file, saved in the searches dir."""
        return await self._require_plugin().search(query)

    async def request(self, line: str) -> Path:
        """Send a `!Bot <file>` line; returns the book, saved in the downloads dir."""
        return await self._require_plugin().request(line)
