"""Unix socket: private location, 0600 permissions, same-user-only connections."""

import asyncio
import json
import os
import socket
import stat
import time

import pytest

import unix_socket as us
from core import (AgentStatus, BotMessage, ConnectionState, Request, RequestKind, RequestState,
                  RequestUpdated)


@pytest.fixture
def runtime_dir(tmp_path, monkeypatch):
    d = tmp_path / "run"
    d.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(d))
    return d


@pytest.fixture
def fake_tmp(tmp_path, monkeypatch):
    """Redirect the fallback location so tests never touch the real /tmp."""
    d = tmp_path / "tmp"
    d.mkdir()
    monkeypatch.setattr(us.tempfile, "gettempdir", lambda: str(d))
    return d


class FakeAgent:
    """The parts of the core interface the socket uses.

    A request ends DONE at `path`, FAILED with `error`, or never if `hang`.
    `bug` is raised from search()/request() themselves, the way a ValueError
    for bad input (or a programming error) would be.
    """

    def __init__(self, path="/books/Dune.epub", error=None, bug=None, hang=False):
        self.path, self.error, self.bug, self.hang = path, error, bug, hang
        self.calls = []
        self.subscribers = []

    def events(self):
        queue = asyncio.Queue()
        self.subscribers.append(queue)

        async def stream():
            # None: the agent stopped, ending every stream
            while (event := await queue.get()) is not None:
                yield event

        return stream()

    def emit(self, event):
        for q in self.subscribers:
            q.put_nowait(event)

    def status(self):
        return AgentStatus(state=ConnectionState.READY, server="irc.example.net", nick="me",
                           channel="#ebooks")

    def search(self, query):
        return self._start("search", query, RequestKind.SEARCH, f"@search {query}")

    def request(self, line):
        return self._start("request", line, RequestKind.BOOK, line)

    def _start(self, call, arg, kind, line):
        self.calls.append((call, arg))
        if self.bug:
            raise self.bug
        req = Request(id="7", kind=kind, line=line, bot=None, state=RequestState.PENDING,
                      created_at=0, updated_at=0)
        self.emit(RequestUpdated(req))
        # Someone else's request: must not reach this client
        self.emit(RequestUpdated(Request(id="99", kind=kind, line="other", bot=None,
                                         state=RequestState.DONE, created_at=0, updated_at=0)))
        if not self.hang:
            asyncio.get_running_loop().call_soon(self._finish, req)
        return req

    def _finish(self, req):
        self.emit(BotMessage("Search", "searching...", req.id))
        self.emit(BotMessage("Mallory", "hi", None))  # not linked to it
        if self.error:
            final = Request(**{**req.__dict__, "state": RequestState.FAILED, "error": self.error})
        else:
            final = Request(**{**req.__dict__, "state": RequestState.DONE, "path": self.path})
        self.emit(RequestUpdated(final))


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


# --- choosing the directory ------------------------------------------------

def test_uses_private_xdg_runtime_dir(runtime_dir):
    assert us.socket_path() == runtime_dir / "ebooks.sock"


@pytest.mark.parametrize("perms", [0o755, 0o750, 0o701])
def test_non_private_xdg_dir_falls_back(runtime_dir, fake_tmp, perms):
    runtime_dir.chmod(perms)
    path = us.socket_path()
    assert path.parent == fake_tmp / f"ebooks-{os.getuid()}"
    assert mode(path.parent) == 0o700


def test_missing_xdg_falls_back_to_private_tmp_dir(monkeypatch, fake_tmp):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    path = us.socket_path()
    assert path.parent == fake_tmp / f"ebooks-{os.getuid()}"
    assert mode(path.parent) == 0o700


def test_xdg_symlink_not_trusted(tmp_path, monkeypatch, fake_tmp):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "link"
    link.symlink_to(real)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(link))
    assert us.socket_path().parent != link


def test_squatted_fallback_dir_refused(monkeypatch, fake_tmp):
    # Someone pre-created our fallback dir with loose permissions
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    squatted = fake_tmp / f"ebooks-{os.getuid()}"
    squatted.mkdir(mode=0o777)
    squatted.chmod(0o777)
    with pytest.raises(us.UnsafeSocketPathError):
        us.socket_path()


def test_fallback_symlink_refused(monkeypatch, fake_tmp, tmp_path):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    (fake_tmp / f"ebooks-{os.getuid()}").symlink_to(elsewhere)
    with pytest.raises(us.UnsafeSocketPathError):
        us.socket_path()


# --- stale files -----------------------------------------------------------

def test_stale_socket_replaced(runtime_dir):
    path = runtime_dir / "ebooks.sock"
    old = socket.socket(socket.AF_UNIX)
    old.bind(str(path))
    old.close()
    us._remove_stale_socket(path)
    assert not path.exists()


def test_non_socket_at_path_not_deleted(runtime_dir):
    path = runtime_dir / "ebooks.sock"
    path.write_text("important")
    with pytest.raises(us.UnsafeSocketPathError):
        us._remove_stale_socket(path)
    assert path.read_text() == "important"


# --- running server ---------------------------------------------------------

async def start_server(agent, path):
    task = asyncio.create_task(us.unix_socket(agent, path))
    for _ in range(100):
        if path.exists():
            break
        await asyncio.sleep(0.01)
    return task


async def stop_server(task):
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def ask_raw(path, data: bytes) -> bytes:
    reader, writer = await asyncio.open_unix_connection(str(path))
    try:
        writer.write(data)
        await writer.drain()
        reply = await reader.read()
    except ConnectionResetError:
        # Server hung up on us without reading (how rejected clients are dropped)
        reply = b""
    writer.close()
    return reply


async def ask(path, command) -> list[dict]:
    """Send one command (a dict, or raw bytes) and return every reply, decoded."""
    data = command if isinstance(command, bytes) else json.dumps(command).encode() + b"\n"
    reply = await ask_raw(path, data)
    return [json.loads(line) for line in reply.splitlines()]


def serve(agent, path, command):
    async def go():
        task = await start_server(agent, path)
        replies = await ask(path, command)
        await stop_server(task)
        return replies
    return asyncio.run(go())


def states(replies):
    return [r["request"]["state"] for r in replies if r["type"] == "RequestUpdated"]


def test_socket_is_0600_and_cleaned_up(runtime_dir):
    path = runtime_dir / "ebooks.sock"

    async def go():
        task = await start_server(FakeAgent(), path)
        assert stat.S_ISSOCK(os.lstat(path).st_mode)
        assert mode(path) == 0o600
        await stop_server(task)

    asyncio.run(go())
    assert not path.exists()


def test_search_streams_its_own_events_until_finished(runtime_dir):
    agent = FakeAgent(path="/searches/dune.txt")
    replies = serve(agent, runtime_dir / "ebooks.sock", {"cmd": "search", "query": "dune"})

    assert agent.calls == [("search", "dune")]
    assert [r["type"] for r in replies] == ["RequestUpdated", "BotMessage", "RequestUpdated"]
    assert states(replies) == ["pending", "done"]
    assert {r.get("request", {}).get("id", "7") for r in replies} == {"7"}
    assert replies[1] == {"type": "BotMessage", "nick": "Search", "text": "searching...",
                          "request_id": "7"}
    assert replies[-1]["request"]["path"] == "/searches/dune.txt"


def test_request_command(runtime_dir):
    agent = FakeAgent()
    replies = serve(agent, runtime_dir / "ebooks.sock",
                    {"cmd": "request", "line": "!Bsk Dune.epub"})
    assert agent.calls == [("request", "!Bsk Dune.epub")]
    assert replies[-1]["request"]["state"] == "done"


def test_failed_request_is_the_final_snapshot(runtime_dir):
    replies = serve(FakeAgent(error="too big"), runtime_dir / "ebooks.sock",
                    {"cmd": "search", "query": "huge"})
    assert replies[-1]["request"]["state"] == "failed"
    assert replies[-1]["request"]["error"] == "too big"


def test_status_command(runtime_dir):
    replies = serve(FakeAgent(), runtime_dir / "ebooks.sock", {"cmd": "status"})
    assert replies == [{"type": "Status", "status": {
        "state": "ready", "server": "irc.example.net", "nick": "me", "channel": "#ebooks",
        "peer": None, "tls_fingerprint": None}}]


@pytest.mark.parametrize("command, error", [
    (b"dune\n", "not a JSON command"),
    (b"\xff\xfe\n", "not a JSON command"),
    (b'["search", "dune"]\n', "unknown command"),
    ({"cmd": "download", "query": "dune"}, "unknown command"),
    ({"query": "dune"}, "unknown command"),
    ({"cmd": "search"}, "search takes cmd, query"),
    ({"cmd": "search", "query": "dune", "extra": 1}, "search takes cmd, query"),
    ({"cmd": "request", "query": "!Bsk x"}, "request takes cmd, line"),
    ({"cmd": "search", "query": 42}, "query must be a string"),
    ({"cmd": "status", "query": "x"}, "status takes cmd"),
])
def test_bad_commands_get_an_error(runtime_dir, command, error):
    agent = FakeAgent()
    replies = serve(agent, runtime_dir / "ebooks.sock", command)
    assert len(replies) == 1 and replies[0]["type"] == "Error"
    assert error in replies[0]["error"]
    assert agent.calls == []


def test_input_the_agent_refuses_gets_its_error(runtime_dir):
    agent = FakeAgent(bug=ValueError("Search query is empty"))
    replies = serve(agent, runtime_dir / "ebooks.sock", {"cmd": "search", "query": " "})
    assert replies == [{"type": "Error", "error": "Search query is empty"}]


def test_unexpected_errors_still_get_a_reply(runtime_dir, caplog):
    agent = FakeAgent(bug=RuntimeError("boom"))
    replies = serve(agent, runtime_dir / "ebooks.sock", {"cmd": "search", "query": "dune"})
    assert replies == [{"type": "Error", "error": "internal error (see agent log)"}]
    assert "boom" in caplog.text


def test_idle_client_times_out(runtime_dir, monkeypatch):
    monkeypatch.setattr(us, "COMMAND_TIMEOUT", 0.05)
    path = runtime_dir / "ebooks.sock"

    async def go():
        task = await start_server(FakeAgent(), path)
        reader, writer = await asyncio.open_unix_connection(str(path))
        # Connect, send nothing, and keep our end open
        reply = await asyncio.wait_for(reader.read(), 1)
        writer.close()
        await stop_server(task)
        return reply

    assert json.loads(asyncio.run(go())) == {"type": "Error", "error": "no command within 0.05s"}


def test_oversized_command_refused(runtime_dir):
    agent = FakeAgent()
    big = json.dumps({"cmd": "search", "query": "x" * us.MAX_COMMAND_BYTES}).encode() + b"\n"
    replies = serve(agent, runtime_dir / "ebooks.sock", big)
    assert replies == [{"type": "Error",
                        "error": f"command longer than {us.MAX_COMMAND_BYTES} bytes"}]
    assert agent.calls == []


def test_shutdown_ends_clients_still_waiting(runtime_dir):
    # A client following a request that never finishes mustn't hold up
    # shutdown (Server.wait_closed() waits for every connection)
    path = runtime_dir / "ebooks.sock"

    async def go():
        task = await start_server(FakeAgent(hang=True), path)
        reader, writer = await asyncio.open_unix_connection(str(path))
        writer.write(b'{"cmd": "search", "query": "dune"}\n')
        first = json.loads(await reader.readline())
        began = time.monotonic()
        await asyncio.wait_for(stop_server(task), 1)
        elapsed = time.monotonic() - began
        rest = await asyncio.wait_for(reader.read(), 1)
        writer.close()
        return first, elapsed, rest

    first, elapsed, rest = asyncio.run(go())
    assert first["request"]["state"] == "pending"
    assert elapsed < 0.5
    assert json.loads(rest) == {"type": "Error", "error": "agent is shutting down"}
    assert not path.exists()


def test_agent_stopping_mid_request_is_reported(runtime_dir):
    # The agent's streams end when it stops, before the request finishes
    path = runtime_dir / "ebooks.sock"
    agent = FakeAgent(hang=True)

    async def go():
        task = await start_server(agent, path)
        reader, writer = await asyncio.open_unix_connection(str(path))
        writer.write(b'{"cmd": "search", "query": "dune"}\n')
        await reader.readline()  # pending
        agent.emit(None)
        rest = await asyncio.wait_for(reader.read(), 1)
        writer.close()
        await stop_server(task)
        return rest

    assert json.loads(asyncio.run(go())) == {"type": "Error", "error": "agent stopped"}


def test_client_hanging_up_early_is_harmless(runtime_dir, caplog):
    path = runtime_dir / "ebooks.sock"
    agent = FakeAgent(hang=True)

    async def go():
        task = await start_server(agent, path)
        _, writer = await asyncio.open_unix_connection(str(path))
        writer.write(b'{"cmd": "search", "query": "dune"}\n')
        await writer.drain()
        await asyncio.sleep(0.05)
        writer.close()
        await asyncio.sleep(0.05)
        # The server is still fine for the next client
        replies = await ask(path, {"cmd": "status"})
        await stop_server(task)
        return replies

    caplog.set_level("INFO")
    assert asyncio.run(go())[0]["type"] == "Status"
    assert "Socket connection failed" not in caplog.text


def test_other_users_rejected(runtime_dir, monkeypatch):
    # Pretend we run as a different uid: the real client then looks like a stranger
    path = runtime_dir / "ebooks.sock"
    agent = FakeAgent()

    async def go():
        task = await start_server(agent, path)
        real_uid = os.getuid()
        monkeypatch.setattr(us.os, "getuid", lambda: real_uid + 1)
        reply = await ask_raw(path, b'{"cmd": "search", "query": "dune"}\n')
        monkeypatch.setattr(us.os, "getuid", lambda: real_uid)
        await stop_server(task)
        return reply

    assert asyncio.run(go()) == b""
    assert agent.calls == []


def test_unsafe_location_is_a_startup_error(runtime_dir):
    path = runtime_dir / "ebooks.sock"
    path.write_text("not a socket")
    with pytest.raises(us.SocketUnavailableError, match="not a socket"):
        asyncio.run(us.unix_socket(FakeAgent(), path))
    assert path.read_text() == "not a socket"
