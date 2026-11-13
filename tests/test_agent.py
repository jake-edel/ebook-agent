"""Connection settings for the IRC bot."""

import asyncio

from server.agent import EbooksAgent


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


def test_irc3_logs_go_to_our_handlers(caplog, tmp_path):
    # irc3's own logging config would print its records to stderr (over the
    # TUI), keep them from the log file, and close the handlers already set up
    import logging

    from config import LogConfig
    from server.logfile import LogFile

    cfg = LogConfig(file=tmp_path / "agent.log")
    with LogFile(cfg, "test") as log_file:
        bot = asyncio.run(_build())
        assert not log_file._handler.stream.closed
        bot.log.error("Connect call failed")
    assert not logging.getLogger("irc3").handlers
    assert "Connect call failed" in caplog.text
    assert "ERROR Connect call failed" in cfg.file.read_text()


async def _build():
    return EbooksAgent()._build_bot()


# --- reconnecting -------------------------------------------------------------

def test_reconnects_wait_longer_each_time(monkeypatch):
    import irc3

    from server import agent as agent_module

    async def go():
        bot = EbooksAgent()._build_bot()
        connects, waits = [], []
        monkeypatch.setattr(irc3.IrcBot, "create_connection",
                            lambda self: connects.append(len(waits)) or self.loop)
        # Record the extra waits instead of waiting
        monkeypatch.setattr(bot.loop, "call_later",
                            lambda delay, callback: waits.append((delay, callback)))
        for _ in range(8):  # the first connect, then irc3 asking to retry
            bot.create_connection()
        extra = [delay for delay, _ in waits]
        # A scheduled retry connects when it comes due...
        waits[0][1]()
        # ...unless the agent has stopped meanwhile
        bot.stopped = True
        waits[1][1]()
        bot.stopped = False
        # Joining the channel starts the delays over
        bot.joined()
        bot.create_connection()
        return connects, extra

    connects, extra = asyncio.run(go())
    irc3_wait = agent_module._IRC3_RETRY_WAIT
    # First connect and first retry at once (irc3 already waited for that one)
    assert connects[:2] == [0, 0]
    assert extra == [d - irc3_wait for d in (10, 30, 60, 120, 300, 300)]
    assert len(connects) == 2 + 1 + 1  # the due retry, then the fresh start after joining
