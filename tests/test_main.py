"""The foreground process: serves the socket until stopped, fails fast without it."""

import asyncio
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest

import ebooks_agent
from config import Config, LogConfig
from server import unix_socket as us

from test_socket import FakeAgent, ask, runtime_dir  # noqa: F401

SEARCH = {"cmd": "search", "query": "dune"}


def test_serves_requests_until_stopped(runtime_dir, caplog):
    path = runtime_dir / "ebooks.sock"
    agent = FakeAgent(path="/books/Dune.epub")

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(ebooks_agent._serve(agent, stop, path))
        for _ in range(100):
            if path.exists():
                break
            await asyncio.sleep(0.01)
        replies = await ask(path, SEARCH)
        stop.set()
        await asyncio.wait_for(task, 1)  # returns cleanly once stopped
        return replies

    caplog.set_level("INFO")
    assert asyncio.run(go())[-1]["request"]["path"] == "/books/Dune.epub"
    assert not path.exists()  # socket removed on shutdown
    assert "Request: @search dune" in caplog.text
    assert "Saved: /books/Dune.epub" in caplog.text


def test_failed_search_is_logged(runtime_dir, caplog):
    path = runtime_dir / "ebooks.sock"
    agent = FakeAgent(error="no offer")

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(ebooks_agent._serve(agent, stop, path))
        for _ in range(100):
            if path.exists():
                break
            await asyncio.sleep(0.01)
        replies = await ask(path, SEARCH)
        stop.set()
        await task
        return replies

    assert asyncio.run(go())[-1]["request"]["error"] == "no offer"
    assert "Failed: @search dune — no offer" in caplog.text


def test_socket_failure_stops_the_process(runtime_dir):
    path = runtime_dir / "ebooks.sock"
    path.write_text("not a socket")

    async def go():
        await ebooks_agent._serve(FakeAgent(), asyncio.Event(), path)

    with pytest.raises(us.SocketUnavailableError):
        asyncio.run(asyncio.wait_for(go(), 1))


def test_socket_path_flag_prints_where_the_agent_listens(runtime_dir):
    # What socket clients run instead of guessing the location
    script = Path(ebooks_agent.__file__)
    out = subprocess.run(
        [sys.executable, str(script), "--socket-path"],
        env={**os.environ, "XDG_RUNTIME_DIR": str(runtime_dir)},
        capture_output=True, text=True, check=True,
    )
    assert out.stdout == f"{runtime_dir / 'ebooks.sock'}\n"
    assert out.stdout.strip() == str(us.socket_path())


def test_refuses_to_start_beside_a_running_agent(runtime_dir, monkeypatch):
    # Checked before connecting to IRC, not after
    connected = []

    class NeverConnect:
        def __init__(self, config):
            connected.append(config)

    monkeypatch.setattr(ebooks_agent, "EbooksAgent", NeverConnect)
    path = runtime_dir / "ebooks.sock"

    async def go():
        first = asyncio.create_task(us.unix_socket(FakeAgent(), path))
        for _ in range(100):
            if path.exists():
                break
            await asyncio.sleep(0.01)
        try:
            with pytest.raises(us.SocketInUseError):
                await ebooks_agent._main(ebooks_agent.Config())
        finally:
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)

    asyncio.run(go())
    assert connected == []


def test_session_is_logged_to_the_file(runtime_dir, monkeypatch, tmp_path):
    # Startup fails (no join in time), but the log file still gets the whole
    # session, with the reason it ended
    class SlowAgent:
        def __init__(self, config):
            pass

        async def start(self):
            logging.getLogger("server.agent").info("Connected to irc.example.net, joining #ebooks")

        async def wait_ready(self, timeout):
            raise TimeoutError("Didn't join #ebooks within 30s")

        async def stop(self):
            pass

    monkeypatch.setattr(ebooks_agent, "EbooksAgent", SlowAgent)
    monkeypatch.setattr(us, "socket_path", lambda: runtime_dir / "ebooks.sock")
    config = Config(log=LogConfig(file=tmp_path / "agent.log"))
    with pytest.raises(TimeoutError):
        asyncio.run(ebooks_agent._main(config, tmp_path / "config.toml"))
    start, connected, end = (tmp_path / "agent.log").read_text().splitlines()
    assert f"session start: ebooks_agent pid {os.getpid()}, config {tmp_path / 'config.toml'}" in start
    assert connected.endswith("INFO  Connected to irc.example.net, joining #ebooks")
    assert end.endswith("session end: TimeoutError: Didn't join #ebooks within 30s")
