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
        bot.include(EbooksPlugin)
        plugin = bot.get_plugin(EbooksPlugin)
        bot.attach_events(
            irc3.event(irc3.rfc.CONNECTED, plugin.on_connected),
            irc3.event(
                r"(?P<mask>\S+) (?:PRIVMSG|NOTICE) (?P<target>\S+) :(?P<text>.*\x01DCC SEND.*)",
                plugin.on_dcc_send,
            ),
        )
        return bot

    async def start(self, timeout: int = 30):
        self._bot = self._build_bot()
        self._plugin = self._bot.get_plugin(EbooksPlugin)
        self._bot.create_connection()
        await asyncio.wait_for(self._plugin._connected.wait(), timeout=timeout)

    def stop(self):
        if self._bot:
            self._bot.quit("bye")

    async def search(self, term: str, timeout: int = 60) -> Path:
        if self._plugin is None:
            raise RuntimeError("Not connected — call start() first")
        return await self._plugin.search(term, timeout=timeout)
