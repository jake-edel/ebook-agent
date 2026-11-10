"""search() and request(): what each sends, and where each saves its file."""

import asyncio
import io
import re
import zipfile

import pytest

import dcc
import plugin
import unix_socket as us
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
        dl, lambda p: p.request("!Oatmeal Dune.epub"), "Oatmeal", "Frank Herbert - Dune.epub"))

    assert p.bot.sent == [("#ebooks", "!Oatmeal Dune.epub")]
    assert path == downloads / "Frank Herbert - Dune.epub"
    assert list(searches.iterdir()) == []


def test_zipped_book_keeps_its_name_inside_the_zip(dl, serve, downloads):
    serve.payload = zip_bytes("inner/Frank Herbert - Dune.epub", b"book")
    _, path = asyncio.run(run_command(
        dl, lambda p: p.request("!Oatmeal Dune.epub"), "Oatmeal", "Dune.zip"))
    # Not named after the "!Oatmeal ..." line any more, and no directories from the zip
    assert path == downloads / "Frank Herbert - Dune.epub"
    assert path.read_bytes() == b"book"
    assert not (downloads / "Dune.zip").exists()


# --- input checks -----------------------------------------------------------

@pytest.mark.parametrize("call", [
    lambda p: p.search(""),
    lambda p: p.search("   "),
    lambda p: p.search("dune\r\nPRIVMSG NickServ :hi"),
    lambda p: p.request("Dune.epub"),       # no !Bot
    lambda p: p.request("!Oatmeal"),        # no file
    lambda p: p.request("!Oatmeal Dune.epub\nQUIT"),
])
def test_bad_input_rejected_before_sending(dl, call, serve):
    p = make_plugin(dl)
    with pytest.raises(ValueError):
        asyncio.run(call(p))
    assert p.bot.sent == []


def test_safe_path_only_into_save_dirs(dl, tmp_path, searches, downloads):
    assert dl.safe_path("a.txt", searches).parent == searches
    assert dl.safe_path("a.txt").parent == downloads
    with pytest.raises(dcc.UnsafeFilenameError):
        dl.safe_path("a.txt", tmp_path)


# --- the socket dispatches on the line ----------------------------------------

class RoutingAgent:
    def __init__(self):
        self.calls = []

    async def search(self, query):
        self.calls.append(("search", query))
        return "results.txt"

    async def request(self, line):
        self.calls.append(("request", line))
        return "book.epub"


@pytest.mark.parametrize("line, expected", [
    ("dune", ("search", "dune")),
    ("@search dune", ("search", "dune")),
    ("@SEARCH dune", ("search", "dune")),
    ("!Oatmeal Dune.epub", ("request", "!Oatmeal Dune.epub")),
])
def test_socket_dispatch(line, expected):
    agent = RoutingAgent()
    asyncio.run(us._dispatch(agent, line))
    assert agent.calls == [expected]


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
