"""Download reliability: failures are reported, nothing is overwritten, nothing hangs."""

import asyncio
import socket
import zipfile

import pytest

import dcc
import unix_socket as us
from dcc import (
    BadArchiveError,
    DownloadError,
    IncompleteDownloadError,
    TransferError,
)

from conftest import local_sender
from test_socket import FakeAgent, ask, runtime_dir, start_server  # noqa: F401


def leftovers(downloads):
    return sorted(p.name for p in downloads.iterdir() if p.name.endswith(".part"))


async def fetch(dl, payload: bytes, size: int, dest, hang=False):
    server, port = await local_sender(payload, hang=hang)
    try:
        return await dl.receive("127.0.0.1", port, size, dest)
    finally:
        server.close()


# --- short and stalled transfers -------------------------------------------

def test_short_transfer_is_a_failure(dl, downloads):
    dest = downloads / "short.epub"
    with pytest.raises(IncompleteDownloadError, match="50 of 100"):
        asyncio.run(fetch(dl, b"x" * 50, 100, dest))
    assert not dest.exists()
    assert leftovers(downloads) == []


def test_incomplete_is_a_download_error():
    # So the CLI and socket report it like any other failure
    assert issubclass(IncompleteDownloadError, DownloadError)


def test_stalled_transfer_times_out(downloads, make_dl):
    dest = downloads / "stall.epub"
    with pytest.raises(TransferError, match="stalled"):
        asyncio.run(fetch(make_dl(stall_timeout=0.2), b"x" * 1000, 1_000_000, dest, hang=True))
    assert not dest.exists()
    assert leftovers(downloads) == []


def test_slow_but_steady_transfer_is_not_cut_off(downloads, make_dl):
    # Total time exceeds the stall timeout, but no single gap does
    dl = make_dl(stall_timeout=0.3)

    async def go():
        async def handle(reader, writer):
            for _ in range(5):
                writer.write(b"y" * 10)
                await writer.drain()
                await asyncio.sleep(0.1)
            writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            return await dl.receive("127.0.0.1", port, 50, downloads / "slow.epub")
        finally:
            server.close()

    assert asyncio.run(go()).read_bytes() == b"y" * 50


# --- connecting -------------------------------------------------------------

def test_connect_timeout(downloads, make_dl, monkeypatch):
    dl = make_dl(connect_timeout=0.1)

    async def never_answers(*args, **kwargs):
        await asyncio.sleep(10)

    monkeypatch.setattr(dcc.asyncio, "open_connection", never_answers)
    with pytest.raises(TransferError, match="within"):
        asyncio.run(dl.receive("127.0.0.1", 5000, 10, downloads / "x.epub"))
    assert list(downloads.iterdir()) == []


def test_connection_refused_is_a_download_error(dl, downloads):
    # Grab a free port, then close it so nothing is listening there
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    with pytest.raises(TransferError, match="Could not connect"):
        asyncio.run(dl.receive("127.0.0.1", port, 10, downloads / "x.epub"))
    assert list(downloads.iterdir()) == []


# --- never overwrite --------------------------------------------------------

def test_existing_file_is_kept_and_new_one_numbered(dl, downloads):
    old = downloads / "Dune.epub"
    old.write_bytes(b"old copy")
    got = asyncio.run(fetch(dl, b"new copy", 8, downloads / "Dune.epub"))
    assert got == downloads / "Dune (1).epub"
    assert got.read_bytes() == b"new copy"
    assert old.read_bytes() == b"old copy"
    assert leftovers(downloads) == []


def test_numbering_skips_taken_names(dl, downloads):
    (downloads / "Dune.epub").write_bytes(b"0")
    (downloads / "Dune (1).epub").write_bytes(b"1")
    got = asyncio.run(fetch(dl, b"2", 1, downloads / "Dune.epub"))
    assert got == downloads / "Dune (2).epub"


def test_failed_download_does_not_destroy_existing_file(dl, downloads):
    old = downloads / "Dune.epub"
    old.write_bytes(b"old copy")
    with pytest.raises(IncompleteDownloadError):
        asyncio.run(fetch(dl, b"x" * 5, 100, downloads / "Dune.epub"))
    assert old.read_bytes() == b"old copy"
    assert sorted(p.name for p in downloads.iterdir()) == ["Dune.epub"]


def test_finalize_never_replaces_a_symlink(downloads, outside):
    # A symlink that appears at the final name (after the up-front path check)
    # is stepped around, not replaced or written through
    link = downloads / "evil.epub"
    link.symlink_to(outside / "nowhere")
    part, f = dcc._open_part(downloads / "evil.epub")
    with f:
        f.write(b"book")
    got = dcc._finalize(part, downloads / "evil.epub")
    assert got == downloads / "evil (1).epub"
    assert got.read_bytes() == b"book"
    assert link.is_symlink()
    assert list(outside.iterdir()) == []


def test_finalize_without_hard_links(dl, downloads, monkeypatch):
    def no_links(*args, **kwargs):
        raise PermissionError(1, "Operation not permitted")

    monkeypatch.setattr(dcc.os, "link", no_links)
    (downloads / "Dune.epub").write_bytes(b"old")
    got = asyncio.run(fetch(dl, b"new", 3, downloads / "Dune.epub"))
    assert got == downloads / "Dune (1).epub"
    assert (downloads / "Dune.epub").read_bytes() == b"old"
    assert leftovers(downloads) == []


def test_extract_does_not_overwrite_existing_book(dl, downloads, make_zip):
    old = downloads / "Dune.epub"
    old.write_bytes(b"old copy")
    zp = make_zip("Dune.zip", b"from zip")
    out = dl.extract(zp, "Dune")
    assert out == downloads / "Dune (1).epub"
    assert out.read_bytes() == b"from zip"
    assert old.read_bytes() == b"old copy"
    assert leftovers(downloads) == []


# --- bad archives -----------------------------------------------------------

def test_corrupt_zip_is_a_download_error(dl, downloads):
    zp = downloads / "junk.zip"
    zp.write_bytes(b"this is not a zip file")
    with pytest.raises(BadArchiveError):
        dl.extract(zp, "junk")


def test_empty_zip_is_a_download_error(dl, downloads):
    zp = downloads / "empty.zip"
    with zipfile.ZipFile(zp, "w"):
        pass
    with pytest.raises(BadArchiveError, match="no files"):
        dl.extract(zp, "empty")


def test_truncated_zip_member_leaves_no_partial_file(dl, downloads, make_zip):
    zp = make_zip("cut.zip", bytes(range(256)) * 4000)
    data = zp.read_bytes()
    # Corrupt the compressed data in the middle, keep the directory intact
    mid = len(data) // 3
    zp.write_bytes(data[:mid] + b"\xff" * 64 + data[mid + 64:])
    with pytest.raises((BadArchiveError, dcc.FileTooLargeError)):
        dl.extract(zp, "cut")
    assert not (downloads / "cut.epub").exists()
    assert leftovers(downloads) == []


# --- callers survive unexpected errors --------------------------------------

def test_socket_reports_unexpected_errors(runtime_dir):
    path = runtime_dir / "ebooks.sock"
    agent = FakeAgent(error=RuntimeError("boom"))

    async def go():
        task = await start_server(agent, path)
        first = await ask(path, b"one\n")
        # The server is still alive afterwards
        agent.error, agent.result = None, "/tmp/ok.epub"
        second = await ask(path, b"two\n")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return first, second

    first, second = asyncio.run(go())
    assert first == b"error: internal error (see agent log)\n"
    assert second == b"/tmp/ok.epub\n"


def test_socket_survives_oversized_line(runtime_dir):
    path = runtime_dir / "ebooks.sock"
    agent = FakeAgent(result="/tmp/ok.epub")

    async def go():
        task = await start_server(agent, path)
        bad = await ask(path, b"x" * 200_000 + b"\n")
        good = await ask(path, b"dune\n")
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        return bad, good

    bad, good = asyncio.run(go())
    assert bad == b""
    assert good == b"/tmp/ok.epub\n"
