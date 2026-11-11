"""The TUI app: browse past searches on disk, and request books from them.

The app runs the agent (and the Unix socket, so Neovim's :Ebook keeps
working) on Textual's event loop, and talks to it only through the core
interface (core.AgentService).
"""

import asyncio
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, OptionList, Static
from textual.widgets.option_list import Option

from core import (
    AgentService,
    AgentStatus,
    BotMessage,
    ConnectionState,
    Event,
    Request,
    RequestKind,
    RequestState,
    RequestUpdated,
    StatusChanged,
)
from results import ResultItem
from unix_socket import SocketUnavailableError, unix_socket

from .library import PastSearch, list_searches

# --- keys -------------------------------------------------------------------
#
# Every binding, in one place. The arrow keys, Enter and Esc work as well
# (Textual's own bindings). Keys are added as features need them.

_MOVE = [
    Binding("j", "cursor_down", "Down", show=False),
    Binding("k", "cursor_up", "Up", show=False),
]
SEARCH_KEYS = _MOVE + [
    Binding("l,right", "select", "Open", show=False),  # as Enter
]
RESULT_KEYS = _MOVE + [
    Binding("h,left,escape", "app.focus_searches", "Back"),
]
APP_KEYS = [
    Binding("q", "quit", "Quit"),
]


# Moving through the searches previews each one, but filling a table with
# 1,000 rows takes a moment: wait until the highlight stops moving
PREVIEW_DELAY = 0.15


# --- widgets ----------------------------------------------------------------

class SearchList(OptionList):
    BINDINGS = SEARCH_KEYS


# The row between the epubs and the other formats
DIVIDER = "divider"


class ResultsTable(DataTable):
    BINDINGS = RESULT_KEYS
    # Where the divider is, if there is one; moving up or down steps over it
    divider_row: int | None = None

    def action_cursor_down(self):
        super().action_cursor_down()
        if self.cursor_row == self.divider_row:
            super().action_cursor_down()

    def action_cursor_up(self):
        super().action_cursor_up()
        if self.cursor_row == self.divider_row:
            super().action_cursor_up()


def human_size(n: int | None) -> str:
    """2_296_381 -> "2.2M"."""
    if n is None:
        return ""
    size = float(n)
    for unit in ("B", "K", "M"):
        if size < 1024:
            break
        size /= 1024
    else:
        unit = "G"
    return f"{size:.0f}{unit}" if unit == "B" or size >= 10 else f"{size:.1f}{unit}"


# --- describing state ---------------------------------------------------------

def describe_status(status: AgentStatus) -> str:
    return {
        ConnectionState.STOPPED: "offline",
        ConnectionState.CONNECTING: f"connecting to {status.server}…",
        ConnectionState.READY: f"{status.channel} as {status.nick}",
        ConnectionState.DISCONNECTED: "disconnected, reconnecting…",
    }[status.state]


def _progress(r: Request) -> int:
    return r.received * 100 // r.size if r.size else 0


def describe_request(r: Request) -> str:
    """One line for the status bar."""
    if r.kind is RequestKind.SEARCH:
        label = f'search "{r.line.removeprefix("@search ")}"'
    else:
        label = r.line.split(" ", 1)[1]  # the file, without the "!Bot"
    match r.state:
        case RequestState.PENDING:
            return f"{label}: waiting to be sent"
        case RequestState.WAITING:
            return f"{label}: sent, waiting for {r.bot or 'an offer'}"
        case RequestState.TRANSFERRING:
            return f"{label}: {_progress(r)}% of {human_size(r.size)}"
        case RequestState.DONE:
            return f"saved {r.path}"
        case RequestState.FAILED:
            return f"{label}: {r.error}"
        case RequestState.CANCELLED:
            return f"{label}: cancelled"


def marker(r: Request | None) -> str:
    """The results table's state column, for the latest request of a row."""
    if r is None:
        return ""
    match r.state:
        case RequestState.PENDING | RequestState.WAITING:
            return "…"
        case RequestState.TRANSFERRING:
            return f"{_progress(r)}%"
        case RequestState.DONE:
            return "✓"
        case RequestState.FAILED:
            return "✗"
        case RequestState.CANCELLED:
            return "–"


# Can be requested again; anything else is in flight or already downloaded
_RETRYABLE = (RequestState.FAILED, RequestState.CANCELLED)


def _search_label(s: PastSearch) -> Text:
    count = len(s.results.items)
    return Text.assemble(
        (s.query, "bold"), "\n",
        (f"{s.when:%b %d %H:%M} · {count} result{'' if count == 1 else 's'}", "dim"),
    )


class EbooksApp(App):
    TITLE = "ebooks"
    BINDINGS = APP_KEYS
    ENABLE_COMMAND_PALETTE = False
    CSS = """
    Horizontal {
        height: 1fr;
    }
    SearchList, ResultsTable {
        height: 100%;
    }
    SearchList {
        width: 34;
        border: round $panel;
    }
    ResultsTable {
        width: 1fr;
        border: round $panel;
    }
    SearchList:focus, ResultsTable:focus {
        border: round $accent;
    }
    #status {
        height: 1;
        padding: 0 1;
        background: $panel;
    }
    """

    def __init__(self, agent: AgentService, searches_dir: Path, socket_path: Path | None = None):
        """`socket_path`: serve the Unix socket there too (None: don't)."""
        super().__init__()
        self.agent = agent
        self.searches_dir = searches_dir
        self.socket_path = socket_path
        self.searches: list[PastSearch] = []
        # The search shown in the results pane, its rows by row key, and the
        # row key of each request line in it
        self.shown: PastSearch | None = None
        self._rows: dict[str, ResultItem] = {}
        self._row_for_line: dict[str, str] = {}
        self._preview = None  # Timer for a pending preview
        # The newest request for each line sent, whoever made it (us, or a
        # socket client like Neovim)
        self._latest: dict[str, Request] = {}
        # The status bar: the connection, then the latest news
        self._connection = "starting…"
        self._note = ""
        self._socket: asyncio.Task | None = None
        self._quit_armed = False
        self._stopped = False
        self._ui_gone = False  # unmounted: nothing left to update

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield SearchList(id="searches")
            yield ResultsTable(id="results", cursor_type="row", zebra_stripes=True)
        yield Static(id="status")
        yield Footer()

    async def on_mount(self):
        sidebar = self.query_one(SearchList)
        sidebar.border_title = "searches"
        self.query_one(ResultsTable).border_title = "results"

        self.searches = list_searches(self.searches_dir)
        sidebar.add_options(Option(_search_label(s), id=str(i)) for i, s in enumerate(self.searches))
        if self.searches:
            sidebar.highlighted = 0
            self.show(self.searches[0])
        else:
            self._note = f"no searches yet in {self.searches_dir}"
        sidebar.focus()

        # Subscribe before starting, so not even the first state change is missed
        self.run_worker(self._follow(self.agent.events()), name="events")
        await self.agent.start()
        if self.socket_path:
            self._socket = asyncio.create_task(self._serve_socket())
        self._refresh_status()

    async def _serve_socket(self):
        try:
            await unix_socket(self.agent, self.socket_path)
        except SocketUnavailableError as e:
            self.note(str(e))

    # --- the agent --------------------------------------------------------

    async def _follow(self, events):
        async for event in events:
            self.on_agent_event(event)

    def on_agent_event(self, event: Event):
        if self._ui_gone:
            return
        if isinstance(event, StatusChanged):
            self._connection = describe_status(event.status)
        elif isinstance(event, RequestUpdated):
            r = event.request
            self._latest[r.line] = r
            if (key := self._row_for_line.get(r.line)) is not None:
                self.query_one(ResultsTable).update_cell(key, "state", marker(r))
            self._note = describe_request(r)
        elif isinstance(event, BotMessage) and event.request_id is not None:
            # Replies to requests; other messages to us wait for a log view
            self._note = f"{event.nick}: {event.text}"
        self._refresh_status()

    def note(self, text: str):
        self._note = text
        self._refresh_status()

    def _refresh_status(self):
        if self._ui_gone:
            return
        text = f"{self._connection} · {self._note}" if self._note else self._connection
        self.query_one("#status", Static).update(text)

    async def action_quit(self):
        active = [r for r in self.agent.requests() if not r.state.finished]
        if active and not self._quit_armed:
            self._quit_armed = True
            n = len(active)
            self.note(f"{n} request{'s' if n > 1 else ''} in progress: press q again to quit and cancel")
            return
        await self._stop_agent()
        self.exit()

    async def _stop_agent(self):
        if self._stopped:
            return
        self._stopped = True
        self._connection = "disconnecting…"
        self._refresh_status()
        if self._socket:
            self._socket.cancel()
            await asyncio.gather(self._socket, return_exceptions=True)
        await self.agent.stop()

    async def on_unmount(self):
        # However the app ends (q normally stops the agent first), leave IRC
        # cleanly. The widgets are already gone by now.
        self._ui_gone = True
        await self._stop_agent()

    # --- searches pane --------------------------------------------------

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted):
        # Moving through the list previews each search's results, once it settles
        if self._preview:
            self._preview.stop()
        search = self.searches[int(event.option.id)]
        self._preview = self.set_timer(PREVIEW_DELAY, lambda: self.show(search))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        if self._preview:
            self._preview.stop()
        self.show(self.searches[int(event.option.id)])
        self.query_one(ResultsTable).focus()

    def show(self, search: PastSearch):
        if search is self.shown:
            return
        self.shown = search
        table = self.query_one(ResultsTable)
        # Columns too, sized here from this search's rows: Textual's own
        # automatic widths don't follow a table that's been refilled
        table.clear(columns=True)
        items = search.results.items
        # Epubs first, then everything else, each in the order the bot sent
        epubs = [i for i in items if i.ext == "epub"]
        others = [i for i in items if i.ext != "epub"]

        def width(header, values):
            return max([len(header), *(len(v) for v in values)])

        # Title last, so a long one is what gets cut off, not the size
        widths = {
            "state": 4,
            "bot": width("bot", (i.bot for i in items)),
            "type": width("type", (i.ext or "" for i in items)),
            "size": 4,
            "title": width("title", (i.title for i in items)),
        }
        table.add_column("", key="state", width=widths["state"])
        table.add_column("bot", key="bot", width=widths["bot"])
        table.add_column("type", key="type", width=widths["type"])
        table.add_column(Text("size", justify="right"), key="size", width=widths["size"])
        table.add_column("title", key="title", width=widths["title"])
        self._rows = {}
        self._row_for_line = {}
        table.divider_row = None
        for item in epubs:
            self._add_row(table, item)
        if epubs and others:
            # Only worth marking where the epubs end if there are any
            table.divider_row = table.row_count
            label = f"── other formats ({len(others)}) "
            title = label + "─" * max(0, widths["title"] - len(label))
            cells = ["─" * widths[k] for k in ("state", "bot", "type", "size")] + [title]
            table.add_row(*(Text(c, style="dim") for c in cells), key=DIVIDER)
        for item in others:
            self._add_row(table, item)
        r = search.results
        table.border_title = f"results: {search.query}"
        parts = [f"{len(r.items)} results"]
        if r.lists_searched is not None:
            parts.append(f"{r.lists_searched} lists")
        if r.unparsed:
            parts.append(f"{len(r.unparsed)} not shown")  # non-standard lines, see results.py
        table.border_subtitle = " · ".join(parts)

    def _add_row(self, table: ResultsTable, item: ResultItem):
        key = str(item.line_no)
        self._rows[key] = item
        self._row_for_line[item.request] = key
        size = Text(human_size(item.size_bytes), justify="right")
        state = marker(self._latest.get(item.request))
        table.add_row(state, item.bot, item.ext or "", size, item.title, key=key)

    # --- results pane ---------------------------------------------------

    def action_focus_searches(self):
        self.query_one(SearchList).focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected):
        if event.row_key.value == DIVIDER:
            return  # e.g. clicked on
        item = self._rows[event.row_key.value]
        current = self._latest.get(item.request)
        if current and current.state is RequestState.DONE:
            self.note(f"already downloaded: {current.path}")
        elif current and current.state not in _RETRYABLE:
            self.note(f"already requested: {item.title}")
        else:
            try:
                self.agent.request(item.request)  # its events update the row
            except ValueError as e:
                self.note(str(e))
