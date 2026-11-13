"""Clean shutdown: stop() sends QUIT and waits for the server to hang up."""

import asyncio
import dataclasses
import time

import pytest

from server.agent import EbooksAgent
from config import Config, IrcConfig
from core import ConnectionState, RequestState, StatusChanged

from conftest import OUR_NICK


class FakeIrcServer:
    """Just enough of an IRC server: welcomes us, records lines, optionally honours QUIT.

    With welcome=False it accepts the connection and then says nothing, like
    a server that hangs during registration.
    """

    def __init__(self, close_on_quit=True, welcome=True):
        self.close_on_quit = close_on_quit
        self.welcome = welcome
        self.lines = []
        self.connections = 0
        self.client_gone = asyncio.Event()

    async def handle(self, reader, writer):
        self.connections += 1
        welcomed = False
        while line := await reader.readline():
            line = line.decode().rstrip("\r\n")
            self.lines.append(line)
            if line.startswith("NICK") and self.welcome and not welcomed:
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
        await agent.wait_ready(2)
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
        await agent.wait_ready(2)
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


def test_start_returns_at_once_and_requests_wait_for_the_join(dl):
    server = FakeIrcServer()

    async def go():
        agent = plaintext_agent(await server.start(), dl)
        events = agent.events()  # subscribing before start() is fine
        await agent.start()
        assert agent.status().state is ConnectionState.CONNECTING
        req = agent.search("dune")  # accepted while still connecting
        await agent.wait_ready(2)
        await asyncio.sleep(0.05)
        sent = [line for line in server.lines if line.startswith("PRIVMSG")]
        await agent.stop()
        seen = [e async for e in events]  # ends by itself after stop()
        server.server.close()
        return req, sent, seen, agent

    req, sent, seen, agent = asyncio.run(go())
    assert req.state is RequestState.PENDING
    assert sent == ["PRIVMSG #ebooks :@search dune"]
    states = [e.status.state for e in seen if isinstance(e, StatusChanged)]
    assert states == [ConnectionState.CONNECTING, ConnectionState.READY, ConnectionState.STOPPED]
    assert agent._bot.attempts == 0  # joined: a later drop starts the reconnect delays over
    assert agent.requests()[0].state is RequestState.CANCELLED


def test_failed_start_can_be_stopped_cleanly(dl):
    # The server accepts but never welcomes us: wait_ready times out, and
    # stop() must leave nothing connecting in the background
    server = FakeIrcServer(welcome=False)

    async def go():
        agent = plaintext_agent(await server.start(), dl)
        await agent.start()
        with pytest.raises(TimeoutError, match="Could not connect"):
            await agent.wait_ready(0.2)
        await agent.stop(timeout=0.2)
        await asyncio.wait_for(server.client_gone.wait(), 1)
        # A retry irc3 had scheduled finds the agent stopped
        agent._bot.create_connection()
        await asyncio.sleep(0.1)
        server.server.close()
        return agent

    agent = asyncio.run(go())
    assert server.connections == 1
    assert agent.status().state is ConnectionState.STOPPED


def test_connection_attempt_finishing_after_stop_is_dropped(dl):
    class Transport:
        closed = False

        def close(self):
            self.closed = True

    async def go():
        agent = EbooksAgent()
        await agent.stop()  # before start() even
        bot = agent._build_bot()
        bot.stopped = True
        attempt = asyncio.get_running_loop().create_future()
        transport = Transport()
        attempt.set_result((transport, object()))
        bot.connection_made(attempt)
        return bot, transport

    bot, transport = asyncio.run(go())
    assert transport.closed
    assert getattr(bot, "protocol", None) is None


def test_agent_cannot_be_restarted():
    async def go():
        agent = EbooksAgent()
        await agent.stop()
        with pytest.raises(RuntimeError, match="once"):
            await agent.start()
        with pytest.raises(RuntimeError, match="stopped"):
            agent.search("dune")

    asyncio.run(go())


def test_whois_shows_the_configured_username_and_realname(dl):
    # Not irc3's defaults, which say "Irc bot based on irc3"
    server = FakeIrcServer()

    async def go():
        agent = plaintext_agent(await server.start(), dl)
        agent._config = dataclasses.replace(agent._config, irc=dataclasses.replace(
            agent._config.irc, username="jake", realname="Jake E."))
        await agent.start()
        await agent.wait_ready(2)
        await agent.stop()
        server.server.close()

    asyncio.run(go())
    assert [line for line in server.lines if line.startswith("USER")] == ["USER jake 0 * :Jake E."]


def test_default_identity_doesnt_mention_irc3(dl):
    server = FakeIrcServer()

    async def go():
        agent = plaintext_agent(await server.start(), dl)
        await agent.start()
        await agent.wait_ready(2)
        await agent.stop()
        server.server.close()

    asyncio.run(go())
    [user] = [line for line in server.lines if line.startswith("USER")]
    defaults = IrcConfig()
    assert user == f"USER {defaults.username} 0 * :{defaults.realname}"
    assert "irc3" not in user
