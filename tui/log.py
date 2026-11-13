"""The log view: what goes to agent.log, live, while the TUI runs.

The agent runs in the TUI's process, so a logging handler on the root logger
sees every record the log file does. LogHandler formats each one and hands
it to the event loop (records can come from other threads, e.g. zip
extraction); LogView shows them, following new ones while you're at the
bottom, the way `tail -f` does.
"""

import asyncio
import logging
import time
from collections.abc import Callable

from rich.table import Table
from rich.text import Text
from textual.binding import Binding
from textual.widgets import RichLog

from .styles import Styles

# As in the log file
_LEVELS = {"WARNING": "WARN", "CRITICAL": "CRIT"}

# This session only; earlier ones are in the file
MAX_LINES = 2000


def record_parts(record: logging.LogRecord, st: Styles) -> tuple[Text, Text, Text]:
    """An entry's time, level and message (with any traceback), styled."""
    level = _LEVELS.get(record.levelname, record.levelname)
    text = record.getMessage()
    if record.exc_info:
        text += "\n" + logging.Formatter().formatException(record.exc_info)
    elif record.exc_text:
        text += "\n" + record.exc_text
    stamp = time.strftime("%H:%M:%S", time.localtime(record.created))
    style = {"WARN": st.warning, "ERROR": st.failed, "CRIT": st.failed + st.strong}.get(level)
    return (Text(stamp, style=st.muted), Text(level, style=style or st.muted),
            Text(text, style=style or ""))


def format_record(record: logging.LogRecord, st: Styles) -> Table:
    """One entry: "20:14:03 WARN  message". The message has a column of its
    own, so its continuation lines (hook output, tracebacks) and a long line
    wrapping line up under it, and each entry starts with its time."""
    grid = Table.grid(padding=(0, 1), expand=True)
    grid.add_column(no_wrap=True)
    grid.add_column(no_wrap=True, min_width=5)
    grid.add_column(ratio=1, overflow="fold")
    grid.add_row(*record_parts(record, st))
    return grid


class LogHandler(logging.Handler):
    """Hands each record, formatted, to `deliver` on the event loop's thread.

    Attach it to the root logger with attach(), and detach() it before the
    widgets go: after that, records are dropped.
    """

    def __init__(self, deliver: Callable[[Table], None], loop: asyncio.AbstractEventLoop,
                 styles: Styles):
        super().__init__(level=logging.INFO)
        self._deliver = deliver
        self._loop = loop
        self._styles = styles
        self._root_level: int | None = None

    def attach(self):
        root = logging.getLogger()
        # INFO, like the log file, even if nothing else asked for it (the
        # file's handler already does this when it's open). Put back on detach.
        self._root_level = root.level
        if root.getEffectiveLevel() > logging.INFO:
            root.setLevel(logging.INFO)
        root.addHandler(self)

    def detach(self):
        root = logging.getLogger()
        root.removeHandler(self)
        if self._root_level is not None:
            root.setLevel(self._root_level)
            self._root_level = None

    def emit(self, record: logging.LogRecord):
        try:
            line = format_record(record, self._styles)
            self._loop.call_soon_threadsafe(self._deliver, line)
        except RuntimeError:
            pass  # the loop has closed: nobody left to show it to
        except Exception:
            self.handleError(record)


class LogView(RichLog):
    """The log, full screen. `j`/`k`, `gg`/`G`; `Tab` or `Esc` goes back."""

    BINDINGS = [
        Binding("j", "scroll_down", "Down", show=False),
        Binding("k", "scroll_up", "Up", show=False),
        Binding("g", "top", "Top", show=False),  # gg: see on_key
        Binding("G", "bottom", "Bottom", show=False),
        Binding("tab,escape", "app.toggle_log", "Back"),
    ]

    def __init__(self, **kwargs):
        # Following is decided per line (see add), not by RichLog
        super().__init__(max_lines=MAX_LINES, wrap=True, auto_scroll=False, **kwargs)
        self._g_pressed = False

    def add(self, entry: Table):
        """Show `entry`, following it if the view was at the bottom."""
        self.write(entry, expand=True, scroll_end=self.is_vertical_scroll_end)

    def on_key(self, event):
        # `gg` is two presses of g; anything else in between cancels it
        if event.key != "g":
            self._g_pressed = False

    def action_top(self):
        if self._g_pressed:
            self.scroll_home(animate=False)
        self._g_pressed = not self._g_pressed

    def action_bottom(self):
        self.scroll_end(animate=False)
