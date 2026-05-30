"""EbooksAgent — owns the IRC connection lifecycle."""

import asyncio
from pathlib import Path

import irc3

from plugin import EbooksPlugin


class EbooksAgent:
    def __init__(self, nick: str = "fierro_viejo"):
        self._nick = nick
        self._bot: irc3.IrcBot | None = None
        self._plugin: EbooksPlugin | None = None

    def _build_bot(self) -> irc3.IrcBot:
        # Configure our server / user settings
        bot = irc3.IrcBot.from_config({
            "nick": self._nick,
            "autojoins": [],
            "host": "irc.irchighway.net",
            "port": 6667,
            "ssl": False,
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
                r"(?P<mask>\S+) (?:PRIVMSG|NOTICE) (?P<target>\S+) :(?P<text>.*\x01DCC SEND.*)",
                plugin.on_dcc_send,
            ),
        )
        # Return a reference to our bot
        return bot

    async def start(self, timeout: int = 30):
        # Create our bot and wait before we do anything else
        self._bot = self._build_bot()
        self._plugin = self._bot.get_plugin(EbooksPlugin)
        self._bot.create_connection()
        async with asyncio.timeout(timeout):
            await self._plugin._connected.wait()

    def stop(self):
        if self._bot:
            self._bot.quit("bye")

    async def search(self, term: str, timeout: int = 60) -> Path:
        if self._plugin is None:
            raise RuntimeError("Not connected — call start() first")
        async with asyncio.timeout(timeout):
            return await self._plugin.search(term)
