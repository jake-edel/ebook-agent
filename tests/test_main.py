"""The foreground process: serves the socket until stopped, fails fast without it."""

import asyncio

import pytest

import ebooks_agent
import unix_socket as us

from test_socket import FakeAgent, ask, runtime_dir  # noqa: F401


def test_serves_requests_until_stopped(runtime_dir, caplog):
    path = runtime_dir / "ebooks.sock"
    agent = FakeAgent(result="/books/Dune.epub")

    async def go():
        stop = asyncio.Event()
        task = asyncio.create_task(ebooks_agent._serve(agent, stop, path))
        for _ in range(100):
            if path.exists():
                break
            await asyncio.sleep(0.01)
        reply = await ask(path, b"dune\n")
        stop.set()
        await asyncio.wait_for(task, 1)  # returns cleanly once stopped
        return reply

    caplog.set_level("INFO")
    assert asyncio.run(go()) == b"/books/Dune.epub\n"
    assert not path.exists()  # socket removed on shutdown
    assert "Request: dune" in caplog.text
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
        reply = await ask(path, b"dune\n")
        stop.set()
        await task
        return reply

    assert asyncio.run(go()) == b"error: no offer\n"
    assert "Failed: dune" in caplog.text


def test_socket_failure_stops_the_process(runtime_dir):
    path = runtime_dir / "ebooks.sock"
    path.write_text("not a socket")

    async def go():
        await ebooks_agent._serve(FakeAgent(), asyncio.Event(), path)

    with pytest.raises(us.SocketUnavailableError):
        asyncio.run(asyncio.wait_for(go(), 1))
