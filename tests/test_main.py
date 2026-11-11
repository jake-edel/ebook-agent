"""The foreground process: serves the socket until stopped, fails fast without it."""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

import ebooks_agent
import unix_socket as us

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
    # What clients like Neovim's :Ebook run instead of guessing the location
    script = Path(ebooks_agent.__file__)
    out = subprocess.run(
        [sys.executable, str(script), "--socket-path"],
        env={**os.environ, "XDG_RUNTIME_DIR": str(runtime_dir)},
        capture_output=True, text=True, check=True,
    )
    assert out.stdout == f"{runtime_dir / 'ebooks.sock'}\n"
    assert out.stdout.strip() == str(us.socket_path())
