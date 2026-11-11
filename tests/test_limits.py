"""Size limits: downloads and zip extraction can't exhaust disk or memory."""

import asyncio
import collections
import struct
import zipfile

import pytest

import dcc
from dcc import BadArchiveError, FileTooLargeError

from conftest import local_sender


def test_sender_cannot_write_past_advertised_size(dl, downloads):
    async def go():
        server, port = await local_sender(b"A" * 300_000)
        dest = downloads / "over.epub"
        await dl.receive("127.0.0.1", port, 100_000, dest)
        server.close()
        return dest

    assert asyncio.run(go()).stat().st_size == 100_000


def test_cancelled_transfer_leaves_no_partial_file(dl, downloads):
    dest = downloads / "partial.epub"

    async def go():
        server, port = await local_sender(b"B" * 50_000, hang=True)
        task = asyncio.create_task(dl.receive("127.0.0.1", port, 1_000_000, dest))
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.close()

    asyncio.run(go())
    assert not dest.exists()


def test_receive_enforces_limit_itself(dl, downloads):
    with pytest.raises(FileTooLargeError):
        asyncio.run(dl.receive("127.0.0.1", 1, dl.max_bytes + 1, downloads / "x.epub"))


def test_low_disk_space_refused_before_connecting(dl, downloads, monkeypatch):
    usage = collections.namedtuple("usage", "total used free")
    monkeypatch.setattr(dcc.shutil, "disk_usage", lambda p: usage(0, 0, 600 * 1024 * 1024))
    # 100 MiB download would leave < 512 MiB headroom. Port 1 is never connected to.
    with pytest.raises(FileTooLargeError, match="disk space"):
        asyncio.run(dl.receive("127.0.0.1", 1, 100 * 1024 * 1024, downloads / "big.epub"))


# --- zip bombs (limit lowered to 1 MiB to keep tests fast) ----------------

@pytest.fixture
def small_limit(make_dl):
    return make_dl(max_size_mib=1)


def test_zip_bomb_refused_from_header(downloads, make_zip, small_limit):
    zp = make_zip("bomb.zip", b"\0" * (50 * 1024 * 1024))  # ~50 KB on disk, 50 MiB inflated
    assert zp.stat().st_size < 100_000
    with pytest.raises(FileTooLargeError):
        small_limit.extract(zp, "bomb")
    assert list(downloads.iterdir()) == []


def test_zip_with_lying_size_header_is_contained(downloads, make_zip, small_limit):
    zp = make_zip("liar.zip", b"\0" * (50 * 1024 * 1024))
    # Rewrite the central directory to claim the entry is only 100 bytes
    data = bytearray(zp.read_bytes())
    i = data.rfind(b"PK\x01\x02")
    data[i + 24:i + 28] = struct.pack("<I", 100)
    zp.write_bytes(data)

    with pytest.raises((FileTooLargeError, BadArchiveError)):
        small_limit.extract(zp, "liar")
    out = downloads / "liar.epub"
    assert not out.exists()


def test_normal_zip_extracts_and_removes_archive(dl, downloads, make_zip):
    zp = make_zip("ok.zip", b"x" * 10_000)
    out = dl.extract(zp, "normal book")
    assert out == downloads / "normal book.epub"
    assert out.stat().st_size == 10_000
    assert not zp.exists()


def test_zip_prefers_ebook_extension(dl, downloads):
    zp = downloads / "multi.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("aaa_readme.nfo", b"junk")
        z.writestr("book.epub", b"the book")
    out = dl.extract(zp, "multi")
    assert out.suffix == ".epub"
    assert out.read_bytes() == b"the book"


def test_zip_with_other_files_is_kept(dl, downloads, caplog):
    # Only one file is extracted, so deleting the zip would lose the rest
    zp = downloads / "both.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("Dune.epub", b"epub")
        z.writestr("Dune.mobi", b"mobi")
    out = dl.extract(zp)
    assert out == downloads / "Dune.epub"
    assert zp.exists()
    assert "1 other file(s)" in caplog.text


def test_directories_in_a_zip_are_not_other_files(dl, downloads):
    zp = downloads / "nested.zip"
    with zipfile.ZipFile(zp, "w") as z:
        z.writestr("Dune/", b"")
        z.writestr("Dune/Dune.epub", b"epub")
    assert dl.extract(zp) == downloads / "Dune.epub"
    assert not zp.exists()
