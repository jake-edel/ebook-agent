"""search() and request(): what each sends, and where each saves its file."""

import asyncio
import io
import re
import zipfile

import pytest

from server import dcc
from server import plugin
from config import ConfigError, load_config

from conftest import OUR_NICK, make_plugin, mask, offer_text

# "<YYYYmmdd-HHMMSS> <query>.<ext>"
_STAMP = r"\d{8}-\d{6}"


def zip_bytes(member: str, payload: bytes) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr(member, payload)
    return buf.getvalue()


@pytest.fixture
def serve(dl, monkeypatch):
    """Fake the DCC transfer: write `serve.payload` to wherever we're told."""
    calls = []

    async def fake_receive(ip, port, size, dest, on_progress=None):
        calls.append(dest)
        # Like the real thing: write a .part, then move it to a free name
        part = dest.with_name(dest.name + ".part")
        part.write_bytes(fake_receive.payload)
        return dcc._finalize(part, dest)

    fake_receive.payload = b"x"
    fake_receive.calls = calls
    monkeypatch.setattr(dl, "receive", fake_receive)
    return fake_receive


async def run_command(dl, coro_fn, sender, filename, size=10):
    p = make_plugin(dl)
    task = asyncio.create_task(coro_fn(p))
    await asyncio.sleep(0.01)
    p.on_dcc_send(mask=mask(sender), target=OUR_NICK, text=offer_text(filename, size=size))
    return p, await asyncio.wait_for(task, 1)


# --- search() ---------------------------------------------------------------

def test_search_sends_at_search_and_saves_results_to_searches(dl, serve, searches, downloads):
    serve.payload = zip_bytes("SearchBot_results_for_ dune.txt", b"results")
    p, path = asyncio.run(run_command(
        dl, lambda p: p.search("dune"), "Search", "SearchBot_results_for_ dune.txt.zip"))

    assert p.bot.sent == [("#ebooks", "@search dune")]
    assert path.parent == searches
    assert re.fullmatch(rf"{_STAMP} dune\.txt", path.name), path.name
    assert path.read_bytes() == b"results"
    assert list(searches.iterdir()) == [path]  # the zip is gone
    assert list(downloads.iterdir()) == []


def test_search_unzipped_txt_is_renamed_too(dl, serve, searches):
    _, path = asyncio.run(run_command(dl, lambda p: p.search("dune"), "Search", "whatever.txt"))
    assert re.fullmatch(rf"{_STAMP} dune\.txt", path.name)
    assert path.parent == searches


def test_search_query_with_slashes_kept_whole(dl, serve, searches):
    _, path = asyncio.run(run_command(dl, lambda p: p.search("AC/DC bio"), "Search", "r.txt"))
    assert path.name.endswith(" AC_DC bio.txt")
    assert path.parent == searches


def test_repeated_search_gets_its_own_file(dl, serve, searches, monkeypatch):
    monkeypatch.setattr(plugin.time, "strftime", lambda fmt: "20261108-120000")

    async def go():
        a = (await run_command(dl, lambda p: p.search("dune"), "Search", "r.txt"))[1]
        b = (await run_command(dl, lambda p: p.search("dune"), "Search", "r.txt"))[1]
        return a, b

    a, b = asyncio.run(go())
    assert (a.name, b.name) == ("20261108-120000 dune.txt", "20261108-120000 dune (1).txt")


# --- request() --------------------------------------------------------------

def test_request_sends_line_and_saves_book_to_downloads(dl, serve, searches, downloads):
    p, path = asyncio.run(run_command(
        dl, lambda p: p.request("!Oatmeal Dune.epub", "Oatmeal"), "Oatmeal", "Frank Herbert - Dune.epub"))

    assert p.bot.sent == [("#ebooks", "!Oatmeal Dune.epub")]
    assert path == downloads / "Frank Herbert - Dune.epub"
    assert list(searches.iterdir()) == []


def test_zipped_book_keeps_its_name_inside_the_zip(dl, serve, downloads):
    serve.payload = zip_bytes("inner/Frank Herbert - Dune.epub", b"book")
    _, path = asyncio.run(run_command(
        dl, lambda p: p.request("!Oatmeal Dune.epub", "Oatmeal"), "Oatmeal", "Dune.zip"))
    # Not named after the "!Oatmeal ..." line any more, and no directories from the zip
    assert path == downloads / "Frank Herbert - Dune.epub"
    assert path.read_bytes() == b"book"
    assert not (downloads / "Dune.zip").exists()


# --- input checks -----------------------------------------------------------

@pytest.mark.parametrize("check, text", [
    (plugin.check_query, ""),
    (plugin.check_query, "   "),
    (plugin.check_query, "dune\r\nPRIVMSG NickServ :hi"),
    (plugin.check_query, "dune\x00"),
    (plugin.check_request, "Dune.epub"),       # no !Bot
    (plugin.check_request, "!Oatmeal"),        # no file
    (plugin.check_request, "!Oatmeal Dune.epub\nQUIT"),
])
def test_bad_input_rejected(check, text):
    # EbooksAgent runs these before anything is sent (see test_service.py)
    with pytest.raises(ValueError):
        check(text)


def test_checks_return_clean_values():
    assert plugin.check_query("  dune  ") == "dune"
    assert plugin.check_request(" !Oatmeal Dune.epub ") == ("!Oatmeal Dune.epub", "Oatmeal")


def test_lines_too_long_for_irc_rejected():
    with pytest.raises(ValueError, match="too long"):
        plugin.check_query("x" * plugin.MAX_LINE_BYTES)  # the "@search " pushes it over
    with pytest.raises(ValueError, match="too long"):
        plugin.check_request("!Bsk " + "é" * (plugin.MAX_LINE_BYTES // 2))  # bytes, not chars
    # Right at the limit is fine
    assert plugin.check_query("x" * (plugin.MAX_LINE_BYTES - len("@search ")))
    line = "!Bsk " + "x" * (plugin.MAX_LINE_BYTES - 5)
    assert plugin.check_request(line) == (line, "Bsk")


def test_safe_path_only_into_save_dirs(dl, tmp_path, searches, downloads):
    assert dl.safe_path("a.txt", searches).parent == searches
    assert dl.safe_path("a.txt").parent == downloads
    with pytest.raises(dcc.UnsafeFilenameError):
        dl.safe_path("a.txt", tmp_path)


# --- config -------------------------------------------------------------------

def test_searches_dir_configurable_and_created(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[downloads]\ndir = "b"\nsearches_dir = "s"\n')
    cfg = load_config(path)
    dl = dcc.Downloads(cfg.downloads)
    dl.create_dirs()
    assert dl.searches_dir == tmp_path / "s"
    assert dl.searches_dir.is_dir()


def test_searches_dir_must_differ_from_downloads(tmp_path):
    with pytest.raises(ConfigError, match="must differ"):
        path = tmp_path / "config.toml"
        path.write_text('[downloads]\ndir = "x"\nsearches_dir = "x"\n')
        load_config(path)


@pytest.mark.parametrize("raw, expected", [("true", True), ("false", False)])
def test_show_private_messages_from_toml(tmp_path, raw, expected):
    path = tmp_path / "config.toml"
    path.write_text(f"[irc]\nshow_private_messages = {raw}\n")
    assert load_config(path).irc.show_private_messages is expected


def test_show_private_messages_must_be_bool(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[irc]\nshow_private_messages = "false"\n')
    with pytest.raises(ConfigError, match="true or false"):
        load_config(path)
