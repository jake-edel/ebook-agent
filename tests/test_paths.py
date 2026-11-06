"""Path traversal: nothing a remote bot sends may write outside DOWNLOADS_DIR."""

import asyncio

import pytest

import dcc
from dcc import UnsafeFilenameError, extract_ebook, receive_dcc, safe_download_path

from conftest import local_sender


@pytest.mark.parametrize("name, expected", [
    ("../../etc/passwd", "passwd"),
    ("/etc/passwd", "passwd"),
    ("..\\..\\win.ini", "win.ini"),
    ("foo/../../bar.epub", "bar.epub"),
    ("dir/sub/book.epub", "book.epub"),
    ("....//....//x", "x"),
    ("\n../x", "x"),
    (".bashrc", "bashrc"),            # no hidden dotfiles
    ("a\x00b.epub", "a_b.epub"),      # NUL and control chars neutralized
    ("Dune - Frank Herbert.epub", "Dune - Frank Herbert.epub"),  # normal names untouched
])
def test_hostile_names_land_inside_downloads(downloads, name, expected):
    path = safe_download_path(name)
    assert path.parent == downloads
    assert path.name == expected


def test_absolute_path_to_outside_dir_is_flattened(downloads, outside):
    path = safe_download_path(str(outside / "pwned.txt"))
    assert path == downloads / "pwned.txt"


@pytest.mark.parametrize("name", ["..", ".", "", "   ", "/", "///", "..."])
def test_unusable_names_rejected(name):
    with pytest.raises(UnsafeFilenameError):
        safe_download_path(name)


def test_long_names_truncated_keeping_extension(downloads):
    path = safe_download_path("x" * 1000 + ".epub")
    assert path.parent == downloads
    assert path.suffix == ".epub"
    assert len(path.name.encode()) <= 200


def test_symlink_in_downloads_is_not_followed(downloads, outside):
    target = outside / "target"
    (downloads / "evil.epub").symlink_to(target)

    with pytest.raises(UnsafeFilenameError):
        safe_download_path("evil.epub")
    # Even if a caller had the path, the open itself refuses the symlink (O_NOFOLLOW)
    with pytest.raises(OSError):
        dcc._open_for_write(downloads / "evil.epub")
    assert not target.exists()


def test_receive_dcc_rejects_raw_outside_path(outside):
    with pytest.raises(UnsafeFilenameError):
        asyncio.run(receive_dcc("127.0.0.1", 1, 10, outside / "raw.epub"))
    assert list(outside.iterdir()) == []


def test_receive_dcc_happy_path(downloads):
    async def go():
        server, port = await local_sender(b"hello world")
        dest = safe_download_path("../../legit book.epub")
        await receive_dcc("127.0.0.1", port, 11, dest)
        server.close()
        return dest

    dest = asyncio.run(go())
    assert dest == downloads / "legit book.epub"
    assert dest.read_bytes() == b"hello world"


def test_zip_member_paths_and_hostile_term_stay_inside(downloads, outside, make_zip):
    zp = make_zip("x.zip", b"book", member="../../../outside.epub")
    out = extract_ebook(zp, f"../../{outside}/term")
    assert out == downloads / "term.epub"
    assert out.read_bytes() == b"book"
    assert list(outside.iterdir()) == []
