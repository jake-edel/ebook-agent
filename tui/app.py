"""The TUI app: search, browse past searches on disk, and request books from them.

The app runs the agent (and the Unix socket, so socket clients keep
working while it's open) on Textual's event loop, and talks to it only
through the core interface (core.AgentService).
"""

import asyncio
from datetime import datetime
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.message import Message
from textual.widgets import DataTable, Footer, Input, Label, OptionList, Static
from textual.widgets.option_list import Option

from core import (
    AgentService,
    AgentStatus,
    BotMessage,
    ConnectionState,
    Event,
    MembersChanged,
    Request,
    RequestKind,
    RequestState,
    RequestUpdated,
    StatusChanged,
)
from results import ResultItem
from unix_socket import SocketUnavailableError, unix_socket

from .library import PastSearch, list_searches, load_search

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
    # Deletes a past search (after a y/n), or dismisses a failed one
    Binding("d", "app.delete_search", "Delete", show=False),
]
RESULT_KEYS = _MOVE + [
    Binding("h,left,escape", "app.focus_searches", "Back"),
]
INPUT_KEYS = [
    Binding("escape", "app.close_search_input", "Cancel"),
]
APP_KEYS = [
    Binding("s", "open_search_input", "Search"),
    Binding("q", "quit", "Quit"),
]


# Moving through the searches previews each one, but filling a table with
# 1,000 rows takes a moment: wait until the highlight stops moving
PREVIEW_DELAY = 0.15


# --- widgets ----------------------------------------------------------------

class SearchList(OptionList):
    BINDINGS = SEARCH_KEYS


class SearchInput(Input):
    """The one-line search prompt. Printable keys are typed, not bindings."""
    BINDINGS = INPUT_KEYS


class ConfirmPrompt(Static, can_focus=True):
    """A y/n question in place of the search prompt. Only y says yes: any
    other key, or focus moving away, says no. Keys don't reach anything else
    while it's open."""

    class Answered(Message):
        def __init__(self, yes: bool):
            super().__init__()
            self.yes = yes

    asking = False

    def ask(self, question: str):
        self.update(Text.assemble(question, " ", ("[y/n]", "bold")))
        self.asking = True
        self.display = True
        self.focus()

    def on_key(self, event):
        event.stop()
        event.prevent_default()
        self._answer(event.key == "y")

    def on_blur(self):
        self._answer(False)

    def _answer(self, yes: bool):
        if not self.asking:
            return
        self.asking = False
        self.post_message(self.Answered(yes))


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


# After an offline bot's name. Its whole row is dimmed too.
OFFLINE_PIP = "○"


def offline_summary(bots: list[str]) -> str:
    """For the results' subtitle: "2 bots offline (artemis_serv, Oatmeal)"."""
    n = len(bots)
    names = ", ".join(bots[:3]) + (", …" if n > 3 else "")
    return f"{n} bot{'' if n == 1 else 's'} offline ({names})"


# Can be requested again; anything else is in flight or already downloaded
_RETRYABLE = (RequestState.FAILED, RequestState.CANCELLED)


# --- the sidebar --------------------------------------------------------------
#
# Each entry is a search on disk, or a search made this session that hasn't
# produced a results file: still running, or failed (kept until dismissed).
# A search that finishes becomes a file on disk like any other.

Entry = PastSearch | Request


def _entry_id(entry: Entry) -> str:
    if isinstance(entry, PastSearch):
        return f"file:{entry.path}"
    return f"search:{entry.id}"


def _entry_time(entry: Entry) -> datetime:
    if isinstance(entry, PastSearch):
        return entry.when
    return datetime.fromtimestamp(entry.created_at)


def search_query(r: Request) -> str:
    return r.line.removeprefix("@search ")


def search_progress(r: Request) -> str:
    """How a search that isn't on disk yet is doing, in a few words."""
    match r.state:
        case RequestState.PENDING:
            return "queued"
        case RequestState.WAITING:
            return "searching…"
        case RequestState.TRANSFERRING:
            return f"receiving {_progress(r)}%"
        case RequestState.DONE:
            return "done"
        case RequestState.FAILED:
            return "✗ failed"
        case RequestState.CANCELLED:
            return "– cancelled"


def _entry_label(entry: Entry) -> Text:
    if isinstance(entry, Request):
        style = "red" if entry.state is RequestState.FAILED else "dim"
        return Text.assemble((search_query(entry), "bold"), "\n", (search_progress(entry), style))
    count = len(entry.results.items)
    return Text.assemble(
        (entry.query, "bold"), "\n",
        (f"{entry.when:%b %d %H:%M} · {count} result{'' if count == 1 else 's'}", "dim"),
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
    #prompt {
        height: 1;
        padding: 0 1;
    }
    #prompt Label {
        color: $text-muted;
    }
    SearchInput, SearchInput:focus {
        border: none;
        height: 1;
        padding: 0 1;
        width: 1fr;
    }
    ConfirmPrompt {
        height: 1;
        padding: 0 1;
        background: $warning-muted;
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
        # The sidebar: searches on disk, and this session's searches that
        # aren't (yet), by request id
        self.searches: list[PastSearch] = []
        self._live: dict[str, Request] = {}
        self._entries: dict[str, Entry] = {}  # by option id
        # The entry shown in the results pane, its rows by row key, and the
        # row key of each request line in it
        self.shown: Entry | None = None
        self._rows: dict[str, ResultItem] = {}
        self._row_for_line: dict[str, str] = {}
        self._rows_for_bot: dict[str, list[str]] = {}  # by lower-cased nick
        # Who's in the channel, lower-cased, or None while we don't know (not
        # joined): then no bot is shown as offline
        self._online: frozenset[str] | None = None
        self._preview = None  # Timer for a pending preview
        self._focus_before_search = None  # to go back to when the prompt closes
        self._deleting: PastSearch | None = None  # waiting for its y/n
        # The newest request for each line sent, whoever made it (us, or a
        # socket client)
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
        with Horizontal(id="prompt"):
            yield Label("@search")
            yield SearchInput(placeholder="title or author, Enter to search, Esc to cancel")
        yield ConfirmPrompt()
        yield Static(id="status")
        yield Footer()

    async def on_mount(self):
        sidebar = self.query_one(SearchList)
        sidebar.border_title = "searches"
        self.query_one(ResultsTable).border_title = "results"

        self.query_one("#prompt").display = False
        self.query_one(ConfirmPrompt).display = False

        self.searches = list_searches(self.searches_dir)
        self._rebuild_sidebar()  # and shows the newest
        if not self.searches:
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
            self._members_changed(event.status.state)
        elif isinstance(event, MembersChanged):
            self._members_changed(self.agent.status().state)
        elif isinstance(event, RequestUpdated):
            r = event.request
            self._latest[r.line] = r
            if (key := self._row_for_line.get(r.line)) is not None:
                self.query_one(ResultsTable).update_cell(key, "state", marker(r))
            self._note = describe_request(r)
            if r.kind is RequestKind.SEARCH:
                self._search_updated(r)
        elif isinstance(event, BotMessage) and event.request_id is not None:
            # Replies to requests; other messages to us wait for a log view
            self._note = f"{event.nick}: {event.text}"
        self._refresh_status()

    def _members_changed(self, state: ConnectionState):
        """Restyle the rows of the bots that came or went."""
        before = self._online
        self._online = self.agent.members() if state is ConnectionState.READY else None
        if self._online == before:
            return
        table = self.query_one(ResultsTable)
        for bot, keys in self._rows_for_bot.items():
            was = before is not None and bot not in before
            if was == self._is_offline(bot):
                continue
            for key in keys:
                item = self._rows[key]
                for column, value in zip(("bot", "type", "size", "title"), self._item_cells(item)):
                    table.update_cell(key, column, value)
        if isinstance(self.shown, PastSearch):
            table.border_subtitle = self._subtitle(self.shown)

    def _is_offline(self, bot: str) -> bool:
        return self._online is not None and bot.lower() not in self._online

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

    def _rebuild_sidebar(self, highlight: str | None = None):
        """Refill the sidebar, newest first, keeping the highlight where it was
        (or moving it to the entry with option id `highlight`)."""
        sidebar = self.query_one(SearchList)
        if highlight is None and (current := sidebar.highlighted_option) is not None:
            highlight = current.id
        old_index = sidebar.highlighted
        entries = sorted([*self.searches, *self._live.values()], key=_entry_time, reverse=True)
        self._entries = {_entry_id(e): e for e in entries}
        with self.prevent(OptionList.OptionHighlighted):
            sidebar.set_options(Option(_entry_label(e), id=oid) for oid, e in self._entries.items())
        if not entries:
            self._show_nothing()
            return
        if highlight in self._entries:
            index = sidebar.get_option_index(highlight)
        else:  # e.g. dismissed: stay put, as far as the list goes
            index = min(old_index or 0, len(entries) - 1)
        with self.prevent(OptionList.OptionHighlighted):
            sidebar.highlighted = index
        # Whatever is highlighted now is what the results pane should show
        self.show(self._entries[sidebar.highlighted_option.id])

    def _search_updated(self, r: Request):
        """A search changed state: follow it in the sidebar, and open its
        results when they arrive."""
        if r.state is RequestState.DONE:
            self._live.pop(r.id, None)
            try:
                search = load_search(r.path)
            except OSError as e:
                self._note = f"search \"{search_query(r)}\": can't read the results: {e}"
                self._rebuild_sidebar()
                return
            self.searches.append(search)
            n = len(search.results.items)
            self._note = f"{search.query}: {n} result{'' if n == 1 else 's'}"
            self._rebuild_sidebar(highlight=_entry_id(search))
            # Into the results, unless the search prompt is open again
            if not self.query_one(SearchInput).has_focus:
                self.query_one(ResultsTable).focus()
            return
        is_new = r.id not in self._live
        self._live[r.id] = r
        if is_new:
            self._rebuild_sidebar()
        else:
            self._entries[_entry_id(r)] = r
            self.query_one(SearchList).replace_option_prompt(_entry_id(r), _entry_label(r))
            if isinstance(self.shown, Request) and self.shown.id == r.id:
                self.show(r)

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted):
        # Moving through the list previews each search's results, once it settles
        if self._preview:
            self._preview.stop()
        entry = self._entries[event.option.id]
        self._preview = self.set_timer(PREVIEW_DELAY, lambda: self.show(entry))

    def on_option_list_option_selected(self, event: OptionList.OptionSelected):
        if self._preview:
            self._preview.stop()
        entry = self._entries[event.option.id]
        if isinstance(entry, Request):
            if entry.state in _RETRYABLE:
                self._retry_search(entry)
            else:
                self.note(f'search "{search_query(entry)}": {search_progress(entry)}')
            return
        self.show(entry)
        self.query_one(ResultsTable).focus()

    def _retry_search(self, failed: Request):
        try:
            self.agent.search(search_query(failed))  # its events add the new entry
        except ValueError as e:
            self.note(str(e))
            return
        self._live.pop(failed.id, None)
        self._rebuild_sidebar()

    def action_delete_search(self):
        option = self.query_one(SearchList).highlighted_option
        entry = self._entries.get(option.id) if option else None
        if entry is None:
            return
        if isinstance(entry, PastSearch):
            self._deleting = entry
            n = len(entry.results.items)
            self.query_one(ConfirmPrompt).ask(
                f'delete search "{entry.query}" ({entry.when:%b %d %H:%M}, '
                f'{n} result{"" if n == 1 else "s"})?')
            return
        # This session's, with no file: nothing to lose, so no question
        if not entry.state.finished:
            self.note(f'search "{search_query(entry)}" is still running')
            return
        del self._live[entry.id]
        self._rebuild_sidebar()

    def on_confirm_prompt_answered(self, event: ConfirmPrompt.Answered):
        self.query_one(ConfirmPrompt).display = False
        self.query_one(SearchList).focus()
        search, self._deleting = self._deleting, None
        if search is None:
            return
        if not event.yes:
            self.note("not deleted")
            return
        try:
            search.path.unlink(missing_ok=True)
        except OSError as e:
            self.note(f"can't delete {search.path.name}: {e}")
            return
        self.searches.remove(search)
        self._rebuild_sidebar()
        self.note(f'deleted search "{search.query}"')

    # --- search prompt --------------------------------------------------

    def action_open_search_input(self):
        prompt = self.query_one("#prompt")
        search_input = self.query_one(SearchInput)
        if not prompt.display:
            self._focus_before_search = self.focused
            search_input.value = ""
            prompt.display = True
        search_input.focus()

    def action_close_search_input(self):
        self.query_one("#prompt").display = False
        before = self._focus_before_search
        (before if before and before.is_attached else self.query_one(SearchList)).focus()

    def on_input_submitted(self, event: Input.Submitted):
        try:
            self.agent.search(event.value)  # its events add it to the sidebar
        except ValueError as e:
            self.note(str(e))  # the prompt stays open, to fix the query
            return
        self.action_close_search_input()

    # --- results pane ---------------------------------------------------

    def show(self, entry: Entry):
        if entry is self.shown:
            return
        if isinstance(entry, Request):
            self._show_live(entry)
            return
        search = entry
        # Columns too, sized here from this search's rows: Textual's own
        # automatic widths don't follow a table that's been refilled
        table = self._clear_results(search)
        items = search.results.items
        # Epubs first, then everything else, each in the order the bot sent
        epubs = [i for i in items if i.ext == "epub"]
        others = [i for i in items if i.ext != "epub"]

        def width(header, values):
            return max([len(header), *(len(v) for v in values)])

        # Title last, so a long one is what gets cut off, not the size
        widths = {
            "state": 4,
            # Room for the pip after an offline bot
            "bot": width("bot", (f"{i.bot} {OFFLINE_PIP}" for i in items)),
            "type": width("type", (i.ext or "" for i in items)),
            "size": 4,
            "title": width("title", (i.title for i in items)),
        }
        table.add_column("", key="state", width=widths["state"])
        table.add_column("bot", key="bot", width=widths["bot"])
        table.add_column("type", key="type", width=widths["type"])
        table.add_column(Text("size", justify="right"), key="size", width=widths["size"])
        table.add_column("title", key="title", width=widths["title"])
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
        table.border_title = f"results: {search.query}"
        table.border_subtitle = self._subtitle(search)

    def _subtitle(self, search: PastSearch) -> str:
        r = search.results
        parts = [f"{len(r.items)} results"]
        if r.lists_searched is not None:
            parts.append(f"{r.lists_searched} lists")
        if r.unparsed:
            parts.append(f"{len(r.unparsed)} not shown")  # non-standard lines, see results.py
        bots = dict.fromkeys(i.bot for i in r.items)  # in the order they appear
        if offline := [b for b in bots if self._is_offline(b)]:
            parts.append(offline_summary(offline))
        return " · ".join(parts)

    def _clear_results(self, shown: Entry | None) -> ResultsTable:
        """Empty the results pane, columns too, for `shown`."""
        self.shown = shown
        table = self.query_one(ResultsTable)
        table.clear(columns=True)
        self._rows = {}
        self._row_for_line = {}
        self._rows_for_bot = {}
        table.divider_row = None
        return table

    def _show_live(self, r: Request):
        """A search with no results file: just how it's doing."""
        table = self._clear_results(r)
        table.border_title = f"results: {search_query(r)}"
        table.border_subtitle = r.error if r.state is RequestState.FAILED else search_progress(r)

    def _show_nothing(self):
        """No searches at all, e.g. the last one was just deleted."""
        table = self._clear_results(None)
        table.border_title = "results"
        table.border_subtitle = None

    def _add_row(self, table: ResultsTable, item: ResultItem):
        key = str(item.line_no)
        self._rows[key] = item
        self._row_for_line[item.request] = key
        self._rows_for_bot.setdefault(item.bot.lower(), []).append(key)
        state = marker(self._latest.get(item.request))
        table.add_row(state, *self._item_cells(item), key=key)

    def _item_cells(self, item: ResultItem) -> tuple[Text, ...]:
        """A row's bot, type, size and title cells: dimmed if the bot is offline.
        (The state column isn't: it's about our request, not the bot.)"""
        offline = self._is_offline(item.bot)
        style = "dim" if offline else ""
        bot = f"{item.bot} {OFFLINE_PIP}" if offline else item.bot
        return (
            Text(bot, style=style),
            Text(item.ext or "", style=style),
            Text(human_size(item.size_bytes), style=style, justify="right"),
            Text(item.title, style=style),
        )

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
        elif self._is_offline(item.bot):
            # It would only wait out the offer timeout
            self.note(f"{item.bot} isn't in {self.agent.status().channel} right now")
        else:
            try:
                self.agent.request(item.request)  # its events update the row
            except ValueError as e:
                self.note(str(e))
