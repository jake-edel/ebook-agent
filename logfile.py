"""The log file: an audit trail of what the agent did, appended to across runs.

Every log record at INFO and above goes to one file (`[log]` in the config),
as plain text, one entry per line, so you can read it with `less` and search it with `grep`:

    2026-11-11 14:03:01.010 INFO  Message sent: @search dune herbert

Each run is bracketed by session lines, so a missing "session end" means the
process crashed or was killed. The file is rotated by size, and created
readable by you only: it holds your searches, the books you asked for and
bots' IP addresses.

Open it only once the socket path is claimed: only one agent runs at a time,
so only one process ever writes or rotates the file.
See docs/logging-plan.md.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from config import LogConfig

# Short enough to keep the columns lined up
_LEVELS = {"WARNING": "WARN", "CRITICAL": "CRIT"}
# Continuation lines (tracebacks, the odd multi-line message) are indented, so
# every entry starts with a timestamp and grep finds them
_INDENT = "    "


class LogFileError(Exception):
    """The log file can't be opened."""


class _Formatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        stamp = self.formatTime(record, "%Y-%m-%d %H:%M:%S") + f".{int(record.msecs):03d}"
        level = _LEVELS.get(record.levelname, record.levelname)
        text = record.getMessage()
        if record.exc_info and not record.exc_text:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            text += "\n" + record.exc_text
        if record.stack_info:
            text += "\n" + self.formatStack(record.stack_info)
        return f"{stamp} {level:<5} {text}".replace("\n", "\n" + _INDENT)


class _PrivateFileHandler(RotatingFileHandler):
    """A RotatingFileHandler whose files are readable by their owner only."""

    def _open(self):
        fd = os.open(self.baseFilename, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        # An existing file keeps its mode: tighten it, e.g. one an older
        # version (or a copy) left world-readable
        os.fchmod(fd, 0o600)
        return open(fd, "a", encoding=self.encoding, errors=self.errors)


class LogFile:
    """The open log file. Use as a context manager, or call close()."""

    def __init__(self, config: LogConfig, program: str, config_path: Path | None = None):
        """Start logging to the file. Raises LogFileError if it can't be opened."""
        self.path = config.file
        try:
            config.file.parent.mkdir(parents=True, exist_ok=True)
            self._handler = _PrivateFileHandler(
                config.file, maxBytes=config.max_size_bytes, backupCount=config.backups,
                encoding="utf-8", errors="backslashreplace",
            )
        except OSError as e:
            raise LogFileError(f"Can't open log file {config.file}: {e.strerror or e}") from None
        self._handler.setFormatter(_Formatter())
        self._handler.setLevel(logging.INFO)
        root = logging.getLogger()
        # The root logger defaults to WARNING; the file wants INFO even when
        # nothing else (the TUI) asked for it. Put back on close.
        self._root_level = root.level
        if root.getEffectiveLevel() > logging.INFO:
            root.setLevel(logging.INFO)
        root.addHandler(self._handler)
        self._closed = False
        self._session_line(
            f"---- session start: {program} pid {os.getpid()}, "
            f"config {config_path or '(built-in defaults)'}, UTC{time.strftime('%z')}"
        )

    def _session_line(self, text: str):
        # Straight to the file: the console doesn't need to see these
        record = logging.makeLogRecord(
            {"name": __name__, "levelno": logging.INFO, "levelname": "INFO", "msg": text})
        self._handler.handle(record)

    def close(self, error: BaseException | None = None):
        """Write the session end line and stop logging to the file.

        `error` is what ended the session, if not a normal shutdown.
        """
        if self._closed:
            return
        self._closed = True
        if error is None:
            self._session_line("---- session end")
        else:
            self._session_line(f"---- session end: {type(error).__name__}: {error}")
        root = logging.getLogger()
        root.removeHandler(self._handler)
        root.setLevel(self._root_level)
        self._handler.close()

    def __enter__(self) -> LogFile:
        return self

    def __exit__(self, exc_type, exc, tb):
        # Ctrl+C is how you stop the agent, not an error. Under asyncio.run
        # it arrives as the main task being cancelled.
        if isinstance(exc, (KeyboardInterrupt, SystemExit, asyncio.CancelledError)):
            exc = None
        self.close(exc)
