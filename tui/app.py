"""The TUI app. Stage 1: browse past searches on disk; nothing talks to IRC yet."""

from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.widgets import DataTable, Footer, OptionList, Static
from textual.widgets.option_list import Option

from results import ResultItem

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


class ResultsTable(DataTable):
    BINDINGS = RESULT_KEYS


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

    def __init__(self, searches_dir: Path):
        super().__init__()
        self.searches_dir = searches_dir
        self.searches: list[PastSearch] = []
        # The search shown in the results pane, and its rows by row key
        self.shown: PastSearch | None = None
        self._rows: dict[str, ResultItem] = {}
        self._preview = None  # Timer for a pending preview

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield SearchList(id="searches")
            yield ResultsTable(id="results", cursor_type="row", zebra_stripes=True)
        yield Static(id="status")
        yield Footer()

    def on_mount(self):
        sidebar = self.query_one(SearchList)
        sidebar.border_title = "searches"
        self.query_one(ResultsTable).border_title = "results"

        self.searches = list_searches(self.searches_dir)
        sidebar.add_options(Option(_search_label(s), id=str(i)) for i, s in enumerate(self.searches))
        if self.searches:
            sidebar.highlighted = 0
            self.show(self.searches[0])
            self.set_status("offline · browsing past searches")
        else:
            self.set_status(f"offline · no searches yet in {self.searches_dir}")
        sidebar.focus()

    def set_status(self, text: str):
        self.query_one("#status", Static).update(text)

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

        def width(header, values):
            return max([len(header), *(len(v) for v in values)])

        # Title last, so a long one is what gets cut off, not the size
        table.add_column("bot", key="bot", width=width("bot", (i.bot for i in items)))
        table.add_column("type", key="type", width=width("type", (i.ext or "" for i in items)))
        table.add_column(Text("size", justify="right"), key="size", width=4)
        table.add_column("title", key="title", width=width("title", (i.title for i in items)))
        self._rows = {}
        for item in items:
            key = str(item.line_no)
            self._rows[key] = item
            size = Text(human_size(item.size_bytes), justify="right")
            table.add_row(item.bot, item.ext or "", size, item.title, key=key)
        r = search.results
        table.border_title = f"results: {search.query}"
        parts = [f"{len(r.items)} results"]
        if r.lists_searched is not None:
            parts.append(f"{r.lists_searched} lists")
        if r.unparsed:
            parts.append(f"{len(r.unparsed)} not shown")  # non-standard lines, see results.py
        table.border_subtitle = " · ".join(parts)

    # --- results pane ---------------------------------------------------

    def action_focus_searches(self):
        self.query_one(SearchList).focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected):
        item = self._rows[event.row_key.value]
        # Requesting books comes in stage 2
        self.set_status(f"offline · would request: {item.request}")
