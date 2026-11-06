"""Connection settings for the IRC bot."""

import asyncio

from agent import EbooksAgent


def test_connects_over_tls():
    async def go():
        return EbooksAgent()._build_bot()

    bot = asyncio.run(go())
    assert bot.config.port == 6697
    assert bot.config.ssl is True
    # irc3 still builds a TLS context; verification is deliberately off
    # because IRCHighway's servers use rotating self-signed certificates
    ctx = bot.get_ssl_context()
    assert ctx is not None
    assert ctx.minimum_version.name in ("TLSv1_2", "TLSv1_3")
