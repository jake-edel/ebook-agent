"""Unix socket: private location, 0600 permissions, same-user-only connections."""

import asyncio
import os
import socket
import stat

import pytest

import unix_socket as us
from core import Request, RequestKind, RequestState


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

    `error` makes requests fail with that message; `bug` is raised from
    search() itself, like a programming error would be.
    """

    def __init__(self, result=None, error=None, bug=None):
        self.result, self.error, self.bug, self.terms = result, error, bug, []

    def search(self, term):
        self.terms.append(term)
        if self.bug:
            raise self.bug
        return Request(id="1", kind=RequestKind.SEARCH, line=f"@search {term}", bot=None,
                       state=RequestState.PENDING, created_at=0, updated_at=0)

    async def wait(self, request_id):
        if self.error:
            return Request(id=request_id, kind=RequestKind.SEARCH, line="", bot=None,
                           state=RequestState.FAILED, created_at=0, updated_at=0, error=self.error)
        return Request(id=request_id, kind=RequestKind.SEARCH, line="", bot=None,
                       state=RequestState.DONE, created_at=0, updated_at=0, path=self.result)


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


async def ask(path, line: bytes) -> bytes:
    reader, writer = await asyncio.open_unix_connection(str(path))
    try:
        writer.write(line)
        await writer.drain()
        data = await reader.read()
    except ConnectionResetError:
        # Server hung up on us without reading (how rejected clients are dropped)
        data = b""
    writer.close()
    return data


def test_socket_is_0600_and_answers_searches(runtime_dir, tmp_path):
    path = runtime_dir / "ebooks.sock"
    agent = FakeAgent(result=tmp_path / "Dune.epub")

    async def go():
        task = await start_server(agent, path)
        assert stat.S_ISSOCK(os.lstat(path).st_mode)
        assert mode(path) == 0o600
        reply = await ask(path, b"dune frank herbert\n")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return reply

    assert asyncio.run(go()) == f"{tmp_path / 'Dune.epub'}\n".encode()
    assert agent.terms == ["dune frank herbert"]
    assert not path.exists()  # cleaned up on shutdown


def test_errors_reported_to_client(runtime_dir):
    path = runtime_dir / "ebooks.sock"
    agent = FakeAgent(error="too big")

    async def go():
        task = await start_server(agent, path)
        reply = await ask(path, b"huge\n")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return reply

    assert asyncio.run(go()) == b"error: too big\n"


def test_other_users_rejected(runtime_dir, monkeypatch):
    # Pretend we run as a different uid: the real client then looks like a stranger
    path = runtime_dir / "ebooks.sock"
    agent = FakeAgent(result="should not be called")

    async def go():
        task = await start_server(agent, path)
        real_uid = os.getuid()
        monkeypatch.setattr(us.os, "getuid", lambda: real_uid + 1)
        reply = await ask(path, b"dune\n")
        monkeypatch.setattr(us.os, "getuid", lambda: real_uid)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return reply

    assert asyncio.run(go()) == b""
    assert agent.terms == []


def test_unsafe_location_is_a_startup_error(runtime_dir):
    path = runtime_dir / "ebooks.sock"
    path.write_text("not a socket")
    with pytest.raises(us.SocketUnavailableError, match="not a socket"):
        asyncio.run(us.unix_socket(FakeAgent(), path))
    assert path.read_text() == "not a socket"
