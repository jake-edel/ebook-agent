"""Clean shutdown: stop() sends QUIT and waits for the server to hang up."""

import asyncio
import time

from agent import EbooksAgent
from config import Config, IrcConfig

from conftest import OUR_NICK


class FakeIrcServer:
    """Just enough of an IRC server: welcomes us, records lines, optionally honours QUIT."""

    def __init__(self, close_on_quit=True):
        self.close_on_quit = close_on_quit
        self.lines = []
        self.connections = 0
        self.client_gone = asyncio.Event()

    async def handle(self, reader, writer):
        self.connections += 1
        welcomed = False
        while line := await reader.readline():
            line = line.decode().rstrip("\r\n")
            self.lines.append(line)
            if line.startswith("NICK") and not welcomed:
                # 001 welcome, then end of MOTD, which irc3 treats as "connected"
                writer.write(f":srv 001 {OUR_NICK} :hi\r\n:srv 376 {OUR_NICK} :End\r\n".encode())
                await writer.drain()
                welcomed = True
            if line.startswith("JOIN"):
                channel = line.split()[1]
                writer.write(f":srv 366 {OUR_NICK} {channel} :End of /NAMES list.\r\n".encode())
                await writer.drain()
            if line.startswith("QUIT") and self.close_on_quit:
                break
        writer.close()
        self.client_gone.set()

    async def start(self):
        self.server = await asyncio.start_server(self.handle, "127.0.0.1", 0)
        return self.server.sockets[0].getsockname()[1]


def plaintext_agent(port, dl):
    """An EbooksAgent pointed at the fake server, without TLS."""
    agent = EbooksAgent(Config(
        irc=IrcConfig(host="127.0.0.1", port=port, nick=OUR_NICK, connect_timeout=2),
        downloads=dl.config,
    ))
    build = agent._build_bot

    def build_plain():
        bot = build()
        bot.config["ssl"] = False
        return bot

    agent._build_bot = build_plain
    return agent


def test_stop_sends_quit_and_waits_for_server(dl, caplog):
    server = FakeIrcServer()

    async def go():
        agent = plaintext_agent(await server.start(), dl)
        await agent.start()
        # irc3 reconnects by scheduling create_connection 2s after a drop it
        # didn't expect. Record what gets scheduled rather than waiting 2s.
        loop = asyncio.get_running_loop()
        scheduled = []
        real_call_later = loop.call_later

        def recording_call_later(delay, callback, *args, **kwargs):
            scheduled.append(callback)
            return real_call_later(delay, callback, *args, **kwargs)

        loop.call_later = recording_call_later

        began = time.monotonic()
        await agent.stop()
        elapsed = time.monotonic() - began
        await asyncio.wait_for(server.client_gone.wait(), 1)
        await asyncio.sleep(0.05)  # let connection_lost run
        server.server.close()
        return agent, elapsed, scheduled

    caplog.set_level("INFO")
    agent, elapsed, scheduled = asyncio.run(go())
    assert "QUIT :bye" in server.lines
    assert elapsed < 1  # returned as soon as the server hung up, not at the timeout
    assert "Disconnected from 127.0.0.1" in caplog.text
    assert agent._bot.create_connection not in scheduled  # no reconnect


def test_stop_gives_up_on_a_server_that_ignores_quit(dl, caplog):
    server = FakeIrcServer(close_on_quit=False)

    async def go():
        agent = plaintext_agent(await server.start(), dl)
        await agent.start()
        began = time.monotonic()
        await agent.stop(timeout=0.3)
        elapsed = time.monotonic() - began
        # We closed our end anyway, so the server sees EOF
        await asyncio.wait_for(server.client_gone.wait(), 1)
        server.server.close()
        return elapsed

    elapsed = asyncio.run(go())
    assert "QUIT :bye" in server.lines
    assert 0.3 <= elapsed < 1
    assert "didn't close the connection" in caplog.text


def test_stop_before_start_is_harmless():
    asyncio.run(EbooksAgent().stop())
