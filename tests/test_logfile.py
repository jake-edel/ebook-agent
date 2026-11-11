"""The log file: format, session lines, permissions and rotation."""

import asyncio
import logging
import re
import stat

import pytest

from config import LogConfig
from logfile import LogFile, LogFileError

log = logging.getLogger("test_logfile")

STAMP = r"\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3}"


@pytest.fixture
def log_config(tmp_path):
    return LogConfig(file=tmp_path / "agent.log")


def lines(path):
    return path.read_text().splitlines()


def mode(path):
    return stat.S_IMODE(path.stat().st_mode)


def test_session_is_bracketed_and_lines_are_timestamped(log_config, tmp_path):
    with LogFile(log_config, "ebooks_agent", tmp_path / "config.toml"):
        log.info("Message sent: @search dune")
        log.warning("Ignoring DCC offer from Foo")
    first, sent, warned, last = lines(log_config.file)
    assert re.fullmatch(
        rf"{STAMP} INFO  ---- session start: ebooks_agent pid \d+, "
        rf"config {re.escape(str(tmp_path / 'config.toml'))}, UTC[+-]\d{{4}}", first)
    assert re.fullmatch(rf"{STAMP} INFO  Message sent: @search dune", sent)
    assert re.fullmatch(rf"{STAMP} WARN  Ignoring DCC offer from Foo", warned)
    assert re.fullmatch(rf"{STAMP} INFO  ---- session end", last)


def test_defaults_only_is_said_so(log_config):
    with LogFile(log_config, "ebooks_tui"):
        pass
    assert ", config (built-in defaults), UTC" in lines(log_config.file)[0]


def test_appends_across_sessions(log_config):
    for n in range(2):
        with LogFile(log_config, "ebooks_agent"):
            log.info("run %d", n)
    text = log_config.file.read_text()
    assert text.count("session start") == 2 and text.count("session end") == 2
    assert text.index("run 0") < text.index("run 1")


def test_debug_is_left_out(log_config):
    with LogFile(log_config, "ebooks_agent"):
        log.debug("noise")
    assert "noise" not in log_config.file.read_text()


def test_traceback_lines_are_indented(log_config):
    with LogFile(log_config, "ebooks_agent"):
        try:
            raise ValueError("boom")
        except ValueError:
            log.exception("'@search dune' failed unexpectedly")
    entry = lines(log_config.file)[1:-1]
    assert re.fullmatch(rf"{STAMP} ERROR '@search dune' failed unexpectedly", entry[0])
    assert len(entry) > 1
    assert all(line.startswith("    ") for line in entry[1:])
    assert entry[-1] == "    ValueError: boom"


def test_session_end_records_an_error(log_config):
    with pytest.raises(TimeoutError):
        with LogFile(log_config, "ebooks_agent"):
            raise TimeoutError("Didn't join #ebooks within 30s")
    assert lines(log_config.file)[-1].endswith(
        "---- session end: TimeoutError: Didn't join #ebooks within 30s")


@pytest.mark.parametrize("exc", [KeyboardInterrupt, asyncio.CancelledError])
def test_ctrl_c_is_a_normal_end(log_config, exc):
    with pytest.raises(exc):
        with LogFile(log_config, "ebooks_agent"):
            raise exc()
    assert lines(log_config.file)[-1].endswith("---- session end")


def test_session_lines_stay_out_of_the_console(log_config, caplog):
    caplog.set_level("INFO")
    with LogFile(log_config, "ebooks_agent"):
        log.info("visible")
    assert "visible" in caplog.text
    assert "session" not in caplog.text


def test_file_is_private(log_config):
    with LogFile(log_config, "ebooks_agent"):
        pass
    assert mode(log_config.file) == 0o600


def test_existing_file_is_tightened(log_config):
    log_config.file.write_text("old\n")
    log_config.file.chmod(0o644)
    with LogFile(log_config, "ebooks_agent"):
        pass
    assert mode(log_config.file) == 0o600
    assert lines(log_config.file)[0] == "old"  # appended to, not replaced


def test_missing_parent_dirs_are_created(tmp_path):
    cfg = LogConfig(file=tmp_path / "logs" / "deep" / "agent.log")
    with LogFile(cfg, "ebooks_agent"):
        pass
    assert cfg.file.is_file()


def test_unopenable_file_is_a_clear_error(tmp_path):
    (tmp_path / "blocker").write_text("")
    cfg = LogConfig(file=tmp_path / "blocker" / "agent.log")
    with pytest.raises(LogFileError, match="Can't open log file .*blocker/agent.log"):
        LogFile(cfg, "ebooks_agent")


def test_rotates_by_size_and_keeps_backups(log_config):
    cfg = LogConfig(file=log_config.file, max_size_mib=1, backups=2)
    line = "x" * 1000
    with LogFile(cfg, "ebooks_agent"):
        for _ in range(3 * 1100):  # a little over 3 MiB
            log.info(line)
    rotated = sorted(p.name for p in cfg.file.parent.glob("agent.log*"))
    assert rotated == ["agent.log", "agent.log.1", "agent.log.2"]
    for name in rotated:
        path = cfg.file.parent / name
        assert path.stat().st_size <= cfg.max_size_bytes
        assert mode(path) == 0o600
    assert lines(cfg.file)[-1].endswith("---- session end")


def test_handler_and_level_are_removed_on_close(log_config):
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    with LogFile(log_config, "ebooks_agent"):
        assert len(root.handlers) == len(handlers) + 1
    assert root.handlers == handlers and root.level == level
    log.info("after")
    assert "after" not in log_config.file.read_text()
