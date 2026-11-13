"""The post-download hook: run on each finished book, safely and unattended."""

import asyncio
import logging
import sys

import pytest

from agent import EbooksAgent
from config import Config, ConfigError, DownloadsConfig, IrcConfig, load_config
from core import ConnectionState, RequestState
from hook import hook_argv, run_hook

from conftest import OUR_NICK, make_plugin, mask, offer_text

PY = sys.executable


def py(code: str) -> tuple[str, ...]:
    """A hook command running a line of Python; the path arrives as sys.argv[1]."""
    return (PY, "-c", code)


# --- argv -------------------------------------------------------------------

def test_path_is_appended_without_a_placeholder(tmp_path):
    path = tmp_path / "a.epub"
    assert hook_argv(("mv", "-t", "/lib"), path) == ["mv", "-t", "/lib", str(path)]


def test_placeholder_is_replaced_in_place(tmp_path):
    path = tmp_path / "a.epub"
    assert hook_argv(("rsync", "{path}", "host:/lib/"), path) == ["rsync", str(path), "host:/lib/"]


def test_placeholder_must_be_the_whole_item(tmp_path):
    # Only an exact "{path}" item is replaced: no string templating
    argv = hook_argv(("echo", "x{path}"), tmp_path / "a.epub")
    assert argv == ["echo", "x{path}", str(tmp_path / "a.epub")]


# --- running ----------------------------------------------------------------

def test_hostile_filename_is_never_run_by_a_shell(tmp_path):
    # Had anything expanded it, the hook would get a name that doesn't exist
    path = tmp_path / "$(echo pwned) `echo pwned` ; ' \".epub"
    path.write_text("book")
    assert asyncio.run(run_hook(("sh", "-c", 'test -f "$1"', "sh"), path, 5))


def test_success_runs_with_path_and_env(tmp_path, caplog):
    path = tmp_path / "Dune.epub"
    path.write_text("book")
    dest = tmp_path / "library"
    dest.mkdir()
    code = ("import os, shutil, sys; assert os.environ['EBOOK_PATH'] == sys.argv[1];"
            f"shutil.move(sys.argv[1], {str(dest)!r}); print('moved')")
    with caplog.at_level(logging.INFO, logger="hook"):
        assert asyncio.run(run_hook(py(code), path, 5))
    assert (dest / "Dune.epub").exists() and not path.exists()
    assert "moved" in caplog.text  # output goes to the log


def test_nonzero_exit_is_logged_not_raised(tmp_path, caplog):
    code = "import sys; print('no route to host', file=sys.stderr); sys.exit(3)"
    assert not asyncio.run(run_hook(py(code), tmp_path / "a.epub", 5))
    assert "status 3" in caplog.text and "no route to host" in caplog.text


def test_missing_command_is_logged_not_raised(tmp_path, caplog):
    assert not asyncio.run(run_hook(("/nonexistent/cmd",), tmp_path / "a.epub", 5))
    assert "failed to start" in caplog.text


def test_stdin_is_empty(tmp_path):
    # An ssh password prompt must fail at once, not hang
    code = "import sys; sys.exit(0 if sys.stdin.read() == '' else 1)"
    assert asyncio.run(run_hook(py(code), tmp_path / "a.epub", 5))


def test_timeout_kills_the_hook(tmp_path, caplog):
    code = "import time; time.sleep(30)"
    assert not asyncio.run(run_hook(py(code), tmp_path / "a.epub", 0.3))
    assert "timed out" in caplog.text


def test_long_output_is_truncated(tmp_path, caplog):
    with caplog.at_level(logging.INFO, logger="hook"):
        assert asyncio.run(run_hook(py("print('x' * 100000)"), tmp_path / "a.epub", 5))
    assert "output truncated" in caplog.text and "x" * 100000 not in caplog.text


# --- the agent --------------------------------------------------------------

@pytest.fixture
def make_agent(dl, received):
    def _make(command):
        downloads = DownloadsConfig(on_complete=command, on_complete_timeout=5)
        a = EbooksAgent(Config(irc=IrcConfig(nick=OUR_NICK), downloads=downloads))
        a._plugin = make_plugin(dl)
        a._attach(a._plugin)
        a._state = ConnectionState.READY
        return a
    return _make


def run_request(agent, start, nick, name):
    async def go():
        req = start()
        await asyncio.sleep(0.01)
        agent._plugin.on_dcc_send(mask=mask(nick), target=OUR_NICK, text=offer_text(name))
        return await agent.wait(req.id)
    return asyncio.run(go())


def test_hook_runs_on_finished_books(make_agent, downloads, tmp_path):
    seen = tmp_path / "seen"
    agent = make_agent(py(f"import sys; open({str(seen)!r}, 'w').write(sys.argv[1])"))
    final = run_request(agent, lambda: agent.request("!Oatmeal Dune.epub"), "Oatmeal", "Dune.epub")
    assert final.state is RequestState.DONE
    # wait() includes the hook, and it saw the book's final path
    assert seen.read_text() == str(downloads / "Dune.epub")


def test_hook_skips_searches(make_agent, tmp_path):
    seen = tmp_path / "seen"
    agent = make_agent(py(f"open({str(seen)!r}, 'w')"))
    final = run_request(agent, lambda: agent.search("dune"), "Search", "r.txt")
    assert final.state is RequestState.DONE and not seen.exists()


def test_failed_hook_leaves_request_done(make_agent, downloads):
    agent = make_agent(py("import sys; sys.exit(1)"))
    final = run_request(agent, lambda: agent.request("!Oatmeal Dune.epub"), "Oatmeal", "Dune.epub")
    assert final.state is RequestState.DONE and (downloads / "Dune.epub").exists()


# --- config -----------------------------------------------------------------

def test_config_reads_a_command_list(tmp_path):
    path = tmp_path / "c.toml"
    path.write_text('[downloads]\non_complete = ["rsync", "{path}", "h:/b/"]\non_complete_timeout = 60\n')
    cfg = load_config(path).downloads
    assert cfg.on_complete == ("rsync", "{path}", "h:/b/") and cfg.on_complete_timeout == 60


@pytest.mark.parametrize("value, message", [
    ('"rsync {path} h:/b/"', "a list of strings"),
    ('["rsync", 1]', "a list of strings"),
    ('[""]', "first item"),
])
def test_config_rejects_bad_commands(tmp_path, value, message):
    path = tmp_path / "c.toml"
    path.write_text(f"[downloads]\non_complete = {value}\n")
    with pytest.raises(ConfigError, match=message):
        load_config(path)
