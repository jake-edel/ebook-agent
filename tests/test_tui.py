"""The TUI: searching, browsing past searches on disk, and requesting books from them."""

import asyncio
import dataclasses
import itertools
import logging
import re
import threading
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

import pytest

from server import plugin
from core import (
    AgentStatus,
    BotMessage,
    ConnectionState,
    HookState,
    MembersChanged,
    Request,
    RequestKind,
    RequestState,
    RequestUpdated,
    StatusChanged,
)
from textual.theme import BUILTIN_THEMES

from tui.app import (
    DIVIDER,
    BookDetails,
    ConfirmPrompt,
    PREVIEW_DELAY,
    EbooksApp,
    ResultsTable,
    SearchInput,
    SearchList,
    book_title,
    human_size,
    marker,
)
from tui.library import list_searches
from tui.log import LogView, record_parts
from tui.styles import Styles

S = RequestState

FIXTURE = Path(__file__).parent / "fixtures" / "search_warhammer.txt"

DUNE = (
    'Search results from SearchBot v3.00.13 by Ook\r\n'
    'Searched 14 lists for "dune frank herbert" , found 2 matches. Enjoy!\r\n'
    '\r\n'
    '!Bsk Frank Herbert - Dune (Retail).epub ::INFO:: 1.02MB\r\n'
    '!Oatmeal Frank Herbert - Dune Messiah.mobi ::INFO:: 680.86KB\r\n'
)


@pytest.fixture
def two_searches(searches):
    """An older warhammer search and a newer dune one, named the way the agent names them."""
    shutil.copy(FIXTURE, searches / "20261108-143005 warhammer.txt")
    (searches / "20261109-091500 dune frank herbert.txt").write_text(DUNE)
    return searches


# --- reading the searches folder ----------------------------------------------

def test_searches_newest_first_with_their_results(two_searches):
    found = list_searches(two_searches)
    assert [s.query for s in found] == ["dune frank herbert", "warhammer"]
    assert found[0].when == datetime(2026, 11, 9, 9, 15)
    assert [i.bot for i in found[0].results.items] == ["Bsk", "Oatmeal"]


def test_query_comes_from_the_header_when_there_is_one(searches):
    # The file name has "/" replaced; the header has the query as sent
    (searches / "20261109-091500 AC_DC.txt").write_text(DUNE.replace("dune frank herbert", "AC/DC"))
    (searches / "20261109-091501 no header (1).txt").write_text("!Bsk a.epub ::INFO:: 1KB\r\n")
    assert sorted(s.query for s in list_searches(searches)) == ["AC/DC", "no header"]


def test_files_not_named_by_the_agent_use_their_mtime(searches):
    path = searches / "copied in.txt"
    path.write_text(DUNE)
    os.utime(path, (0, 1_700_000_000))
    [found] = list_searches(searches)
    assert found.when == datetime.fromtimestamp(1_700_000_000)


def test_only_results_files_are_listed(searches):
    (searches / "kept.txt.zip").write_bytes(b"PK")
    (searches / "x.txt.1a2b3c4d.part").write_text("partial")
    assert list_searches(searches) == []


def test_missing_folder_has_no_searches(tmp_path):
    assert list_searches(tmp_path / "nope") == []


@pytest.mark.parametrize("n, text", [
    (None, ""), (0, "0B"), (1023, "1023B"), (1024, "1.0K"), (696_360, "680K"),
    (2_296_381, "2.2M"), (47 * 1024**2, "47M"), (5 * 1024**3, "5.0G"),
])
def test_human_size(n, text):
    assert human_size(n) == text


# --- the app ----------------------------------------------------------------

class FakeAgent:
    """An AgentService the tests drive: requests only change when a test says so."""

    def __init__(self):
        self.state = ConnectionState.STOPPED
        self.made: list[str] = []      # lines passed to request()
        self.searched: list[str] = []  # queries passed to search()
        self._requests: dict[str, Request] = {}
        self._ids = itertools.count(1)
        self._queues: list[asyncio.Queue] = []
        self.stopped = False
        # Who's in the channel; every bot the tests' searches use, by default
        self.online = {"bsk", "oatmeal", "artemis_serv", "ashurbanipal"}

    def members(self):
        # Like the real agent: nobody, unless joined
        return frozenset(self.online) if self.state is ConnectionState.READY else frozenset()

    def set_members(self, *nicks):
        self.online = {n.lower() for n in nicks}
        self.emit(MembersChanged())

    def status(self):
        return AgentStatus(state=self.state, server="irc.example.net", nick="me", channel="#ebooks")

    def emit(self, event):
        for q in self._queues:
            q.put_nowait(event)

    def set_state(self, state):
        self.state = state
        self.emit(StatusChanged(self.status()))

    def events(self):
        queue = asyncio.Queue()
        self._queues.append(queue)

        async def stream():
            while (event := await queue.get()) is not None:
                yield event

        return stream()

    async def start(self):
        self.set_state(ConnectionState.CONNECTING)
        self.set_state(ConnectionState.READY)

    async def stop(self):
        for r in self.requests():
            if not r.state.finished:
                self.update(r.id, state=S.CANCELLED)
        self.stopped = True
        self.set_state(ConnectionState.STOPPED)
        self.emit(None)

    def requests(self):
        return tuple(self._requests.values())

    def request(self, line):
        line, bot = plugin.check_request(line)  # the real checks
        self.made.append(line)
        return self.add(Request(id=str(next(self._ids)), kind=RequestKind.BOOK, line=line,
                                bot=bot, state=S.PENDING, created_at=time.time(),
                                updated_at=time.time()))

    def search(self, query):
        query = plugin.check_query(query)  # the real checks
        self.searched.append(query)
        return self.add(Request(id=str(next(self._ids)), kind=RequestKind.SEARCH,
                                line=f"@search {query}", bot=None, state=S.PENDING,
                                created_at=time.time(), updated_at=time.time()))

    def add(self, req):
        self._requests[req.id] = req
        self.emit(RequestUpdated(req))
        return req

    def update(self, request_id, **changes):
        return self.add(dataclasses.replace(self._requests[request_id], **changes))


def run_app(searches_dir, steps, size=(120, 30), agent=None):
    """Run the app headless, call `steps(app, pilot)`, and return what it returns."""
    async def go():
        app = EbooksApp(agent or FakeAgent(), searches_dir)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            return await steps(app, pilot)
    return asyncio.run(go())


def status(app):
    return str(app.query_one("#status").render())


def state_cells(app):
    return [str(row[0]) for row in rows(app)]


def rows(app):
    """The results table's rows, without the divider."""
    table = app.query_one(ResultsTable)
    return [[str(c) for c in table.get_row(r.key)]
            for r in table.ordered_rows if r.key.value != DIVIDER]


def bots(app):
    """Which bot each row is from (there's no column for it), without the divider."""
    table = app.query_one(ResultsTable)
    return [app._rows[r.key.value].bot for r in table.ordered_rows if r.key.value != DIVIDER]


def dimmed_bots(app):
    """The bot of each dimmed row, without the divider."""
    table = app.query_one(ResultsTable)
    keys = [r.key for r in table.ordered_rows if r.key.value != DIVIDER]
    return [app._rows[k.value].bot for k in keys
            if all(c.style == app.text_styles.muted for c in table.get_row(k)[1:])]


def has_divider(app):
    return app.query_one(ResultsTable).divider_row is not None


async def settle(pilot):
    # Past the preview delay
    await pilot.pause(PREVIEW_DELAY + 0.1)


def test_opens_on_the_newest_search(two_searches):
    async def steps(app, pilot):
        return app.focused, rows(app), app.query_one("#status").render()

    focused, shown, bar = run_app(two_searches, steps)
    assert isinstance(focused, SearchList)
    assert [(r[1], str(r[2]), r[3]) for r in shown] == [
        ("epub", "1.0M", "Frank Herbert - Dune (Retail)"),
        ("mobi", "681K", "Frank Herbert - Dune Messiah"),
    ]
    assert str(bar) == "#ebooks as me"


def test_moving_through_searches_previews_them(two_searches):
    async def steps(app, pilot):
        await pilot.press("j")
        await settle(pilot)
        table = app.query_one(ResultsTable)
        after_j = (app.shown.query, len(rows(app)), table.border_subtitle)
        await pilot.press("k")
        await settle(pilot)
        return after_j, app.shown.query

    (query, count, subtitle), back = run_app(two_searches, steps)
    assert (query, count) == ("warhammer", 10)
    assert subtitle == "10 results · 14 lists"
    assert back == "dune frank herbert"


def test_preview_waits_for_the_highlight_to_settle(two_searches):
    async def steps(app, pilot):
        await pilot.press("j")
        before = app.shown.query  # moved, but not drawn yet
        await settle(pilot)
        return before, app.shown.query

    assert run_app(two_searches, steps) == ("dune frank herbert", "warhammer")


@pytest.mark.parametrize("open_key, back_key", [("enter", "h"), ("l", "escape"), ("right", "left")])
def test_into_the_results_and_back(two_searches, open_key, back_key):
    async def steps(app, pilot):
        await pilot.press(open_key)
        in_results = app.focused
        await pilot.press("j")
        cursor = app.query_one(ResultsTable).cursor_row
        await pilot.press(back_key)
        return in_results, cursor, app.focused

    in_results, cursor, back = run_app(two_searches, steps)
    assert isinstance(in_results, ResultsTable)
    assert cursor == 2  # the first mobi: j stepped over the divider
    assert isinstance(back, SearchList)


def test_enter_opens_the_highlighted_search_at_once(two_searches):
    async def steps(app, pilot):
        await pilot.press("j", "enter")  # no wait for the preview
        return app.shown.query, isinstance(app.focused, ResultsTable)

    assert run_app(two_searches, steps) == ("warhammer", True)


def test_no_searches_yet(searches):
    async def steps(app, pilot):
        return rows(app), status(app)

    shown, bar = run_app(searches, steps)
    assert shown == []
    assert "no searches yet" in bar


def test_q_quits_and_stops_the_agent(two_searches):
    agent = FakeAgent()

    async def steps(app, pilot):
        await pilot.press("enter")  # from the results pane too
        await pilot.press("q")
        await pilot.pause()
        return app.is_running

    assert run_app(two_searches, steps, agent=agent) is False
    assert agent.stopped


# --- epubs first ---------------------------------------------------------------

def test_epubs_first_then_a_divider_then_the_rest(two_searches):
    async def steps(app, pilot):
        await pilot.press("j")
        await settle(pilot)
        table = app.query_one(ResultsTable)
        divider = table.get_row_at(table.divider_row)
        return list(zip(bots(app), (r[1] for r in rows(app)))), table.divider_row, str(divider[3])

    shown, divider_row, label = run_app(two_searches, steps)
    # The fixture lists two artemis_serv mobis first; each group keeps the bot's order
    assert shown == ([("Bsk", "epub")] * 5 + [("artemis_serv", "mobi")] * 2
                     + [("Ashurbanipal", "pdf")] * 2 + [("Bsk", "mobi")])
    assert divider_row == 5
    assert label.startswith("── other formats (5) ──")


@pytest.mark.parametrize("ext", ["epub", "mobi"])
def test_no_divider_with_only_one_kind(searches, ext):
    (searches / "20261109-091500 one kind.txt").write_text(
        f"!Bsk a.{ext} ::INFO:: 1MB\r\n!Bsk b.{ext} ::INFO:: 1MB\r\n")

    async def steps(app, pilot):
        return has_divider(app), len(rows(app)), app.query_one(ResultsTable).row_count

    assert run_app(searches, steps) == (False, 2, 2)


def test_cursor_steps_over_the_divider(two_searches):
    # dune: one epub, the divider, one mobi
    async def steps(app, pilot):
        await pilot.press("enter")
        table = app.query_one(ResultsTable)
        seen = []
        for key in ("j", "down", "k", "up"):
            await pilot.press(key)
            seen.append(table.cursor_row)
        return seen

    assert run_app(two_searches, steps) == [2, 2, 0, 0]


def test_enter_on_the_divider_does_nothing(two_searches):
    agent = FakeAgent()

    async def steps(app, pilot):
        await pilot.press("enter")
        table = app.query_one(ResultsTable)
        table.move_cursor(row=table.divider_row)  # e.g. by clicking it
        await pilot.press("enter")
        await pilot.pause()
        return agent.made

    assert run_app(two_searches, steps, agent=agent) == []


# --- the connection -----------------------------------------------------------

def test_status_bar_follows_the_connection(two_searches):
    agent = FakeAgent()

    async def steps(app, pilot):
        seen = [status(app)]
        for state in (ConnectionState.DISCONNECTED, ConnectionState.CONNECTING,
                      ConnectionState.READY):
            agent.set_state(state)
            await pilot.pause()
            seen.append(status(app))
        return seen

    assert run_app(two_searches, steps, agent=agent) == [
        "#ebooks as me", "disconnected, reconnecting…", "connecting to irc.example.net…",
        "#ebooks as me"]


# --- requesting books ---------------------------------------------------------

def test_enter_on_a_result_requests_it(two_searches):
    agent = FakeAgent()

    async def steps(app, pilot):
        await pilot.press("enter", "j", "enter")
        await pilot.pause()
        return state_cells(app), status(app)

    cells, bar = run_app(two_searches, steps, agent=agent)
    assert agent.made == ["!Oatmeal Frank Herbert - Dune Messiah.mobi"]
    assert cells == ["", "…"]
    assert bar == "#ebooks as me · Frank Herbert - Dune Messiah.mobi: waiting to be sent"


def test_a_request_is_followed_to_the_end(two_searches):
    agent = FakeAgent()

    async def steps(app, pilot):
        await pilot.press("enter", "enter")
        await pilot.pause()
        [req] = agent.requests()
        seen = []
        for changes in (
            dict(state=S.WAITING),
            dict(state=S.TRANSFERRING, filename="Dune.epub", size=1_000_000),
            dict(received=420_000),
            dict(state=S.DONE, path=Path("/books/Dune.epub")),
        ):
            agent.update(req.id, **changes)
            await pilot.pause()
            seen.append((state_cells(app)[0], status(app).split(" · ", 1)[1]))
        return seen

    assert run_app(two_searches, steps, agent=agent) == [
        ("…", "Frank Herbert - Dune (Retail).epub: sent, waiting for Bsk"),
        ("0%", "Frank Herbert - Dune (Retail).epub: 0% of 977K"),
        ("42%", "Frank Herbert - Dune (Retail).epub: 42% of 977K"),
        ("✓", "saved /books/Dune.epub"),
    ]


def test_no_second_request_while_one_is_in_flight_or_done(two_searches):
    agent = FakeAgent()

    async def steps(app, pilot):
        await pilot.press("enter", "enter")
        await pilot.pause()
        await pilot.press("enter")
        in_flight = status(app)
        [req] = agent.requests()
        agent.update(req.id, state=S.DONE, path=Path("/books/Dune.epub"))
        await pilot.press("enter")
        return in_flight, status(app)

    in_flight, done = run_app(two_searches, steps, agent=agent)
    assert len(agent.made) == 1
    assert in_flight.endswith("already requested: Frank Herbert - Dune (Retail)")
    assert done.endswith("already downloaded: /books/Dune.epub")


@pytest.mark.parametrize("ending", [dict(state=S.FAILED, error="No DCC offer"),
                                    dict(state=S.CANCELLED)])
def test_a_failed_request_can_be_retried(two_searches, ending):
    agent = FakeAgent()

    async def steps(app, pilot):
        await pilot.press("enter", "enter")
        await pilot.pause()
        agent.update(agent.requests()[0].id, **ending)
        await pilot.pause()
        cell = state_cells(app)[0]
        await pilot.press("enter")
        await pilot.pause()
        return cell, state_cells(app)[0]

    before, after = run_app(two_searches, steps, agent=agent)
    assert before == marker(Request(id="x", kind=RequestKind.BOOK, line="", bot=None,
                                    created_at=0, updated_at=0, **ending))
    assert after == "…"
    assert len(agent.made) == 2


def test_failure_reason_is_shown(two_searches):
    agent = FakeAgent()

    async def steps(app, pilot):
        await pilot.press("enter", "enter")
        await pilot.pause()
        agent.update(agent.requests()[0].id, state=S.FAILED, error="No DCC offer received within 60s")
        await pilot.pause()
        return status(app)

    assert run_app(two_searches, steps, agent=agent).endswith(
        "Frank Herbert - Dune (Retail).epub: No DCC offer received within 60s")


def test_state_column_survives_switching_searches(two_searches):
    agent = FakeAgent()

    async def steps(app, pilot):
        await pilot.press("enter", "enter")   # request the first dune result
        await pilot.press("h", "j", "enter")  # over to warhammer...
        await pilot.press("h", "k", "enter")  # ...and back
        return state_cells(app)

    assert run_app(two_searches, steps, agent=agent) == ["…", ""]


def test_requests_from_elsewhere_show_on_their_rows(two_searches):
    # e.g. a socket client: the app serves the socket too
    agent = FakeAgent()

    async def steps(app, pilot):
        agent.add(Request(id="9", kind=RequestKind.BOOK,
                          line="!Oatmeal Frank Herbert - Dune Messiah.mobi", bot="Oatmeal",
                          state=S.TRANSFERRING, created_at=0, updated_at=0, size=10, received=5))
        await pilot.pause()
        return state_cells(app)

    assert run_app(two_searches, steps, agent=agent) == ["", "50%"]


def test_bot_replies_to_requests_are_shown(two_searches):
    agent = FakeAgent()

    async def steps(app, pilot):
        await pilot.press("enter", "enter")
        await pilot.pause()
        agent.emit(BotMessage("Mallory", "hey", None))  # not about a request: not shown
        agent.emit(BotMessage("Bsk", "You are in queue position 2", agent.requests()[0].id))
        await pilot.pause()
        return status(app)

    assert run_app(two_searches, steps, agent=agent) == "#ebooks as me · Bsk: You are in queue position 2"


def test_requests_refused_by_the_agent_say_why(searches):
    (searches / "20261109-091500 long.txt").write_text(
        f"!Bsk {'x' * plugin.MAX_LINE_BYTES}.epub ::INFO:: 1MB\r\n")

    async def steps(app, pilot):
        await pilot.press("enter", "enter")
        return status(app)

    assert "too long for IRC" in run_app(searches, steps)


def test_quitting_with_a_request_in_flight_asks_first(two_searches):
    agent = FakeAgent()

    async def steps(app, pilot):
        await pilot.press("enter", "enter")
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()
        first = (app.is_running, status(app))
        await pilot.press("q")
        await pilot.pause()
        return first, app.is_running

    (running, bar), after = run_app(two_searches, steps, agent=agent)
    assert running and bar.endswith("1 request in progress: press q again to quit and cancel")
    assert after is False
    assert agent.stopped and agent.requests()[0].state is S.CANCELLED


# --- searching ----------------------------------------------------------------

AC_DC = (
    'Searched 14 lists for "ac/dc" , found 1 matches. Enjoy!\r\n'
    '\r\n'
    '!Bsk AC_DC - Highway to Hell.epub ::INFO:: 1MB\r\n'
)


def sidebar(app):
    """The sidebar's entries, as their two lines of text."""
    options = app.query_one(SearchList).options
    return [tuple(str(o.prompt).split("\n")) for o in options]


def highlighted(app):
    sidebar_ = app.query_one(SearchList)
    return str(sidebar_.get_option_at_index(sidebar_.highlighted).prompt).split("\n")[0]


def prompt_open(app):
    return app.query_one("#prompt").display


def finish_search(agent, req, searches_dir, contents=AC_DC, name="20261111-120000 ac_dc.txt"):
    """What the agent does when a search's results arrive."""
    path = searches_dir / name
    path.write_text(contents)
    agent.update(req.id, state=S.DONE, path=path)


def test_s_opens_the_prompt_and_esc_closes_it(two_searches):
    async def steps(app, pilot):
        await pilot.press("enter", "s")  # from the results pane
        opened = (prompt_open(app), isinstance(app.focused, SearchInput))
        await pilot.press("escape")
        return opened, prompt_open(app), app.focused

    (opened, focused), closed, back = run_app(two_searches, steps)
    assert opened and focused
    assert not closed
    assert isinstance(back, ResultsTable)  # back where you were


def test_enter_sends_the_search(two_searches):
    agent = FakeAgent()

    async def steps(app, pilot):
        await pilot.press("s", *"ac/dc", "enter")
        await pilot.pause()
        return prompt_open(app), sidebar(app)[0], app.focused

    is_open, top, focused = run_app(two_searches, steps, agent=agent)
    assert agent.searched == ["ac/dc"]
    assert not is_open
    assert top == ("ac/dc", "queued")
    assert isinstance(focused, SearchList)


def test_keys_type_into_the_prompt_instead_of_acting(two_searches):
    agent = FakeAgent()

    async def steps(app, pilot):
        await pilot.press("s", *"qsdjk", "enter")
        await pilot.pause()
        return app.is_running

    assert run_app(two_searches, steps, agent=agent)
    assert agent.searched == ["qsdjk"]


def test_a_rejected_query_keeps_the_prompt_open(two_searches):
    agent = FakeAgent()

    async def steps(app, pilot):
        await pilot.press("s", "space", "enter")
        return prompt_open(app), status(app)

    is_open, bar = run_app(two_searches, steps, agent=agent)
    assert agent.searched == []
    assert is_open and bar.endswith("Search query is empty")


def test_a_search_progresses_through_its_states(two_searches):
    agent = FakeAgent()

    async def steps(app, pilot):
        await pilot.press("s", *"ac/dc", "enter")
        await pilot.pause()
        [req] = agent.requests()
        seen = []
        for changes in (
            dict(state=S.WAITING),
            dict(state=S.TRANSFERRING, filename="results.zip", size=1000),
            dict(received=400),
        ):
            agent.update(req.id, **changes)
            await pilot.pause()
            seen.append((sidebar(app)[0][1],))
        return seen

    assert [s[0] for s in run_app(two_searches, steps, agent=agent)] == [
        "searching…", "receiving 0%", "receiving 40%"]


def test_its_results_open_when_they_arrive(two_searches):
    agent = FakeAgent()

    async def steps(app, pilot):
        await pilot.press("s", *"ac/dc", "enter")
        await pilot.pause()
        finish_search(agent, agent.requests()[0], two_searches)
        await pilot.pause()
        return sidebar(app), highlighted(app), app.shown.query, rows(app), app.focused, status(app)

    entries, high, shown, shown_rows, focused, bar = run_app(two_searches, steps, agent=agent)
    assert entries[0] == ("ac/dc", "Nov 11 12:00 · 1 result")
    assert len(entries) == 3  # the placeholder became the search on disk
    assert high == shown == "ac/dc"
    assert [r[3] for r in shown_rows] == ["AC_DC - Highway to Hell"]
    assert isinstance(focused, ResultsTable)
    assert bar.endswith("ac/dc: 1 result")


def test_results_arriving_while_typing_dont_take_the_focus(two_searches):
    agent = FakeAgent()

    async def steps(app, pilot):
        await pilot.press("s", *"ac/dc", "enter")
        await pilot.pause()
        await pilot.press("s", *"du")
        finish_search(agent, agent.requests()[0], two_searches)
        await pilot.pause()
        await pilot.press(*"ne")
        return app.shown.query, app.query_one(SearchInput).value, app.focused

    shown, typed, focused = run_app(two_searches, steps, agent=agent)
    assert shown == "ac/dc"
    assert typed == "dune"
    assert isinstance(focused, SearchInput)


def test_the_pane_shows_a_running_search_when_its_highlighted(two_searches):
    agent = FakeAgent()

    async def steps(app, pilot):
        await pilot.press("s", *"ac/dc", "enter")
        await pilot.pause()
        app.query_one(SearchList).highlighted = 0
        await settle(pilot)
        table = app.query_one(ResultsTable)
        before = (table.border_title, table.border_subtitle, rows(app))
        agent.update(agent.requests()[0].id, state=S.WAITING)
        await pilot.pause()
        return before, table.border_subtitle

    (title, subtitle, shown), after = run_app(two_searches, steps, agent=agent)
    assert (title, subtitle, shown) == ("results: ac/dc", "queued", [])
    assert after == "searching…"


def test_a_failed_search_stays_until_dismissed(two_searches):
    agent = FakeAgent()

    async def steps(app, pilot):
        await pilot.press("s", *"ac/dc", "enter")
        await pilot.pause()
        agent.update(agent.requests()[0].id, state=S.FAILED, error="No DCC offer received within 60s")
        await pilot.pause()
        app.query_one(SearchList).highlighted = 0
        await settle(pilot)
        failed = (sidebar(app)[0], app.query_one(ResultsTable).border_subtitle, status(app))
        await pilot.press("d")
        return failed, sidebar(app), highlighted(app)

    (top, subtitle, bar), after, high = run_app(two_searches, steps, agent=agent)
    assert top == ("ac/dc", "✗ failed")
    assert subtitle == "No DCC offer received within 60s"
    assert bar.endswith('search "ac/dc": No DCC offer received within 60s')
    assert [e[0] for e in after] == ["dune frank herbert", "warhammer"]
    assert high == "dune frank herbert"


def test_enter_on_a_failed_search_runs_it_again(two_searches):
    agent = FakeAgent()

    async def steps(app, pilot):
        await pilot.press("s", *"ac/dc", "enter")
        await pilot.pause()
        agent.update(agent.requests()[0].id, state=S.FAILED, error="nope")
        await pilot.pause()
        app.query_one(SearchList).highlighted = 0
        await pilot.press("enter")
        await pilot.pause()
        return sidebar(app)

    entries = run_app(two_searches, steps, agent=agent)
    assert agent.searched == ["ac/dc", "ac/dc"]
    assert [e for e in entries if e[0] == "ac/dc"] == [("ac/dc", "queued")]


def test_running_searches_cant_be_dismissed(two_searches):
    agent = FakeAgent()

    async def steps(app, pilot):
        await pilot.press("s", *"ac/dc", "enter")
        await pilot.pause()
        app.query_one(SearchList).highlighted = 0
        await pilot.press("d")
        return len(sidebar(app)), status(app)

    count, bar = run_app(two_searches, steps, agent=agent)
    assert count == 3
    assert bar.endswith('search "ac/dc" is still running')


def test_searches_from_elsewhere_appear_too(two_searches):
    # e.g. a search made by a socket client
    agent = FakeAgent()

    async def steps(app, pilot):
        agent.add(Request(id="9", kind=RequestKind.SEARCH, line="@search ac/dc", bot=None,
                          state=S.WAITING, created_at=time.time(), updated_at=time.time()))
        await pilot.pause()
        return sidebar(app)[0]

    assert run_app(two_searches, steps, agent=agent) == ("ac/dc", "searching…")


def test_first_search_with_none_on_disk(searches):
    agent = FakeAgent()

    async def steps(app, pilot):
        await pilot.press("s", *"ac/dc", "enter")
        await pilot.pause()
        finish_search(agent, agent.requests()[0], searches)
        await pilot.pause()
        return [e[0] for e in sidebar(app)], app.shown.query

    assert run_app(searches, steps, agent=agent) == (["ac/dc"], "ac/dc")


# --- offline bots -------------------------------------------------------------

def test_offline_bots_are_dimmed(two_searches):
    agent = FakeAgent()
    agent.online = {"bsk"}  # not Oatmeal

    async def steps(app, pilot):
        table = app.query_one(ResultsTable)
        return dimmed_bots(app), table.border_subtitle

    dimmed, subtitle = run_app(two_searches, steps, agent=agent)
    assert dimmed == ["Oatmeal"]
    assert subtitle == "2 results · 14 lists · 1 bot offline (Oatmeal)"


def test_rows_follow_bots_coming_and_going(two_searches):
    agent = FakeAgent()

    async def steps(app, pilot):
        table = app.query_one(ResultsTable)
        agent.set_members("Oatmeal")
        await pilot.pause()
        gone = dimmed_bots(app), table.border_subtitle
        agent.set_members("Oatmeal", "Bsk")
        await pilot.pause()
        return gone, (dimmed_bots(app), table.border_subtitle)

    gone, back = run_app(two_searches, steps, agent=agent)
    assert gone == (["Bsk"], "2 results · 14 lists · 1 bot offline (Bsk)")
    assert back == ([], "2 results · 14 lists")


def test_nobody_is_offline_while_not_joined(two_searches):
    # members() is empty then: that means "don't know", not "all gone"
    agent = FakeAgent()
    agent.online = {"bsk"}

    async def steps(app, pilot):
        before = dimmed_bots(app)
        agent.set_state(ConnectionState.DISCONNECTED)
        await pilot.pause()
        during = dimmed_bots(app), app.query_one(ResultsTable).border_subtitle
        agent.set_state(ConnectionState.READY)
        await pilot.pause()
        return before, during, dimmed_bots(app)

    before, during, after = run_app(two_searches, steps, agent=agent)
    assert before == after == ["Oatmeal"]
    assert during == ([], "2 results · 14 lists")


def test_switching_searches_keeps_bots_offline(two_searches):
    agent = FakeAgent()
    agent.online = {"bsk", "ashurbanipal"}

    async def steps(app, pilot):
        await pilot.press("j")
        await settle(pilot)
        return dimmed_bots(app), app.query_one(ResultsTable).border_subtitle

    dimmed, subtitle = run_app(two_searches, steps, agent=agent)
    assert dimmed == ["artemis_serv"] * 2
    assert subtitle == "10 results · 14 lists · 1 bot offline (artemis_serv)"


def test_offline_bots_cant_be_requested(two_searches):
    agent = FakeAgent()
    agent.online = {"bsk"}

    async def steps(app, pilot):
        await pilot.press("enter", "j", "enter")  # into the results, the Oatmeal mobi
        await pilot.pause()
        return agent.made, state_cells(app), status(app)

    made, states, bar = run_app(two_searches, steps, agent=agent)
    assert made == []
    assert states == ["", ""]
    assert bar == "#ebooks as me · Oatmeal isn't in #ebooks right now"


def test_offline_summary_names_at_most_three():
    from tui.app import offline_summary
    assert offline_summary(["a"]) == "1 bot offline (a)"
    assert offline_summary(["a", "b", "c", "d"]) == "4 bots offline (a, b, c, …)"


# --- deleting past searches -----------------------------------------------------

def test_d_asks_before_deleting_a_search(two_searches):
    async def steps(app, pilot):
        await pilot.press("d")
        prompt = app.query_one(ConfirmPrompt)
        return prompt.display, str(prompt.render()), app.focused

    shown, question, focused = run_app(two_searches, steps)
    assert shown
    assert question == 'delete search "dune frank herbert" (Nov 09 09:15, 2 results)? [y/n]'
    assert isinstance(focused, ConfirmPrompt)


def test_y_deletes_the_search_and_its_file(two_searches):
    async def steps(app, pilot):
        await pilot.press("d", "y")
        await pilot.pause()
        return (sidebar(app), highlighted(app), app.shown.query, app.focused,
                app.query_one(ConfirmPrompt).display, status(app))

    entries, high, shown, focused, prompt_shown, bar = run_app(two_searches, steps)
    assert [p.name for p in two_searches.iterdir()] == ["20261108-143005 warhammer.txt"]
    assert [e[0] for e in entries] == ["warhammer"]
    assert high == shown == "warhammer"  # the next one along, shown in the results
    assert isinstance(focused, SearchList)
    assert not prompt_shown
    assert bar.endswith('deleted search "dune frank herbert"')


@pytest.mark.parametrize("answer", ["n", "escape", "j", "d"])
def test_anything_but_y_keeps_it(two_searches, answer):
    async def steps(app, pilot):
        await pilot.press("d", answer)
        await pilot.pause()
        return sidebar(app), app.focused, app.query_one(ConfirmPrompt).display, status(app)

    entries, focused, prompt_shown, bar = run_app(two_searches, steps)
    assert len(list(two_searches.iterdir())) == 2
    assert [e[0] for e in entries] == ["dune frank herbert", "warhammer"]
    assert isinstance(focused, SearchList) and not prompt_shown
    assert bar.endswith("not deleted")


@pytest.mark.parametrize("key", ["q", "s", "tab"])
def test_other_keys_dont_act_while_asking(two_searches, key):
    async def steps(app, pilot):
        await pilot.press("d", key)
        await pilot.pause()
        return app.is_running, app.query_one("#prompt").display, app.query_one(LogView).display

    running, search_prompt, log_open = run_app(two_searches, steps)
    # q didn't quit, s didn't open the search prompt, tab didn't open the log
    assert running and not search_prompt and not log_open


def test_deleting_the_last_search(searches):
    (searches / "20261109-091500 dune.txt").write_text(DUNE)

    async def steps(app, pilot):
        await pilot.press("d", "y")
        await pilot.pause()
        return sidebar(app), rows(app)

    assert run_app(searches, steps) == ([], [])
    assert list(searches.iterdir()) == []


def test_a_file_already_gone_is_still_removed(two_searches):
    async def steps(app, pilot):
        (two_searches / "20261109-091500 dune frank herbert.txt").unlink()
        await pilot.press("d", "y")
        await pilot.pause()
        return [e[0] for e in sidebar(app)]

    assert run_app(two_searches, steps) == ["warhammer"]


# --- books ----------------------------------------------------------------------

DUNE_LINE = "!Bsk Frank Herbert - Dune (Retail).epub"
MESSIAH_LINE = "!Oatmeal Frank Herbert - Dune Messiah.mobi"
H = HookState


def book(agent, line=DUNE_LINE, created_at=None, **changes):
    """A book request made elsewhere (e.g. by a socket client), then `changes`."""
    req = agent.request(line)
    if created_at is not None:
        req = agent.update(req.id, created_at=created_at)
    return agent.update(req.id, **changes) if changes else req


def headings_and_entries(app):
    """The sidebar's lines, headings included, the way they read: "BOOKS",
    "✓ Frank Herbert - Dune (Retail) | 20:14 · 1.0M · moved"."""
    return [" | ".join(line.strip() for line in str(o.prompt).split("\n"))
            for o in app.query_one(SearchList).options]


def details(app):
    """The book pane's text, laid out 100 columns wide."""
    from rich.console import Console
    console = Console(width=100, record=True, file=open(os.devnull, "w"))
    console.print(app.query_one(BookDetails).content)
    return console.export_text()


def test_book_titles_drop_the_bot_and_ids():
    r = Request(id="1", kind=RequestKind.BOOK, bot="artemis_serv", state=S.PENDING, created_at=0,
                updated_at=0, line="!artemis_serv f73222c7950c | The Purging of Kadillus.mobi")
    assert book_title(r) == "The Purging of Kadillus"


def test_no_books_no_sections(two_searches):
    async def steps(app, pilot):
        return headings_and_entries(app), app.query_one(SearchList).border_title

    lines, title = run_app(two_searches, steps)
    assert "BOOKS" not in lines and "SEARCHES" not in lines
    assert title == "searches"


def test_a_requested_book_appears_above_the_searches(two_searches):
    agent = FakeAgent()

    async def steps(app, pilot):
        await pilot.press("enter", "enter")  # into dune's results, request the epub
        await pilot.pause()
        return headings_and_entries(app), highlighted(app), app.focused, \
            app.query_one(SearchList).border_title

    lines, high, focused, title = run_app(two_searches, steps, agent=agent)
    assert lines[:3] == ["BOOKS", "… Frank Herbert - Dune (Retail) | queued for Bsk", "SEARCHES"]
    assert [l.split(" | ")[0] for l in lines[3:]] == ["dune frank herbert", "warhammer"]
    # Nothing moved under you: still on the search, still in its results
    assert high == "dune frank herbert" and isinstance(focused, ResultsTable)
    assert title is None  # the headings say it now


def test_newest_book_first_and_finishing_doesnt_move_it(two_searches):
    agent = FakeAgent()

    async def steps(app, pilot):
        old = book(agent, created_at=1000.0)
        book(agent, MESSIAH_LINE, created_at=2000.0)
        await pilot.pause()
        before = headings_and_entries(app)[1:3]
        agent.update(old.id, state=S.DONE, path=Path("/books/Dune.epub"))
        await pilot.pause()
        return before, headings_and_entries(app)[1:3]

    before, after = run_app(two_searches, steps, agent=agent)
    assert [l.split(" | ")[0] for l in before] == [
        "… Frank Herbert - Dune Messiah", "… Frank Herbert - Dune (Retail)"]
    assert [l.split(" | ")[0] for l in after] == [
        "… Frank Herbert - Dune Messiah", "✓ Frank Herbert - Dune (Retail)"]


def test_a_book_is_followed_through_its_hook(two_searches):
    agent = FakeAgent()
    created = datetime(2026, 11, 12, 20, 14).timestamp()

    async def steps(app, pilot):
        req = book(agent, created_at=created)
        seen = []
        for changes in (
            dict(state=S.WAITING),
            dict(state=S.TRANSFERRING, filename="Dune.epub", size=1_000_000, received=420_000),
            dict(state=S.DONE, path=Path("/home/me/downloads/Dune.epub"), received=1_000_000,
                 hook=H.RUNNING),
            dict(hook=H.DONE),
        ):
            agent.update(req.id, **changes)
            await pilot.pause()
            seen.append((headings_and_entries(app)[1], status(app).split(" · ", 1)[1]))
        return seen

    assert run_app(two_searches, steps, agent=agent) == [
        ("… Frank Herbert - Dune (Retail) | waiting for Bsk",
         "Frank Herbert - Dune (Retail).epub: sent, waiting for Bsk"),
        ("⇣ Frank Herbert - Dune (Retail) | Bsk · 42% of 977K",
         "Frank Herbert - Dune (Retail).epub: 42% of 977K"),
        ("⇢ Frank Herbert - Dune (Retail) | moving…",
         "Frank Herbert - Dune (Retail).epub: downloaded, moving…"),
        ("✓ Frank Herbert - Dune (Retail) | 20:14 · 977K · moved",
         "Frank Herbert - Dune (Retail).epub: moved"),
    ]


@pytest.mark.parametrize("changes, entry, bar, cell", [
    (dict(hook=H.FAILED, hook_error="exit 23: connection refused"),
     "⚠ Frank Herbert - Dune (Retail) | hook failed · in downloads/",
     "Frank Herbert - Dune (Retail).epub: hook failed (exit 23: connection refused), "
     "still at /home/me/downloads/Dune.epub", "⚠"),
    (dict(),  # no hook configured
     "✓ Frank Herbert - Dune (Retail) | 20:14 · 977K · saved",
     "saved /home/me/downloads/Dune.epub", "✓"),
])
def test_how_a_download_ended(two_searches, changes, entry, bar, cell):
    agent = FakeAgent()
    created = datetime(2026, 11, 12, 20, 14).timestamp()

    async def steps(app, pilot):
        book(agent, created_at=created, state=S.DONE, size=1_000_000, received=1_000_000,
             path=Path("/home/me/downloads/Dune.epub"), **changes)
        await pilot.pause()
        return headings_and_entries(app)[1], status(app).split(" · ", 1)[1], state_cells(app)[0]

    assert run_app(two_searches, steps, agent=agent) == (entry, bar, cell)


def test_results_rows_follow_the_hook_too(two_searches):
    agent = FakeAgent()

    async def steps(app, pilot):
        req = book(agent, state=S.DONE, path=Path("/d/Dune.epub"), hook=H.RUNNING)
        await pilot.pause()
        moving = state_cells(app)[0]
        agent.update(req.id, hook=H.DONE)
        await pilot.pause()
        await pilot.press("enter", "enter")  # try to request it again
        return moving, state_cells(app)[0], status(app)

    moving, moved, bar = run_app(two_searches, steps, agent=agent)
    assert (moving, moved) == ("⇢", "✓")
    assert bar.endswith("already downloaded, and moved: Frank Herbert - Dune (Retail)")


def test_moving_through_the_sidebar_skips_the_headings(two_searches):
    agent = FakeAgent()

    async def steps(app, pilot):
        book(agent)
        await pilot.pause()
        seen = [highlighted(app)]  # the newest search, as before
        for key in ("k", "k", "j", "j"):
            await pilot.press(key)
            seen.append(highlighted(app))
        return seen

    assert run_app(two_searches, steps, agent=agent) == [
        "dune frank herbert",
        "… Frank Herbert - Dune (Retail)",   # over SEARCHES
        "… Frank Herbert - Dune (Retail)",   # BOOKS can't be reached
        "dune frank herbert",
        "warhammer",
    ]


def test_highlighting_a_book_shows_its_details(two_searches):
    agent = FakeAgent()
    created = datetime(2026, 11, 12, 20, 14, 3).timestamp()

    async def steps(app, pilot):
        req = book(agent, created_at=created, state=S.WAITING)
        agent.emit(BotMessage("Bsk", "You are in queue position 2", req.id))
        agent.emit(BotMessage("Mallory", "hi", None))  # not about it
        await pilot.press("k")
        await settle(pilot)
        table, pane = app.query_one(ResultsTable), app.query_one(BookDetails)
        first = (table.display, pane.display, pane.border_title, pane.border_subtitle, details(app))
        agent.update(req.id, state=S.DONE, size=1_000_000, path=Path("/d/Dune.epub"),
                     hook=H.FAILED, hook_error="exit 23: connection refused")
        await pilot.pause()
        after = details(app)
        await pilot.press("j")  # back to a search: its results again
        await settle(pilot)
        return first, after, (table.display, pane.display, len(rows(app)))

    (table_shown, pane_shown, title, subtitle, text), after, back = run_app(
        two_searches, steps, agent=agent)
    assert (table_shown, pane_shown) == (False, True)
    assert title == "book: Frank Herbert - Dune (Retail)" and subtitle == "waiting for Bsk"
    assert "request    !Bsk Frank Herbert - Dune (Retail).epub" in text
    assert "state      sent, waiting for Bsk" in text
    assert "requested  Nov 12 20:14:03" in text
    assert "Bsk: You are in queue position 2" in text and "Mallory" not in text
    # It follows the book while shown
    assert "hook       failed: exit 23: connection refused" in after
    assert "still at   /d/Dune.epub" in after and "size       977K" in after
    assert back == (True, False, 2)


def test_enter_on_a_book_only_says_how_its_doing(two_searches):
    agent = FakeAgent()

    async def steps(app, pilot):
        book(agent, state=S.WAITING)
        await pilot.press("k", "enter")
        await pilot.pause()
        return app.focused, status(app), agent.made

    focused, bar, made = run_app(two_searches, steps, agent=agent)
    assert isinstance(focused, SearchList)
    assert bar.endswith("Frank Herbert - Dune (Retail).epub: sent, waiting for Bsk")
    assert made == [DUNE_LINE]  # not requested again


@pytest.mark.parametrize("changes, note", [
    (dict(state=S.TRANSFERRING, size=10, received=5), "is still in progress"),
    (dict(state=S.DONE, path=Path("/d/Dune.epub"), hook=H.RUNNING), "is still moving"),
])
def test_books_in_progress_cant_be_dismissed(two_searches, changes, note):
    agent = FakeAgent()

    async def steps(app, pilot):
        book(agent, **changes)
        await pilot.press("k", "d")
        await pilot.pause()
        return len(app._books), app.query_one(ConfirmPrompt).display, status(app)

    count, asked, bar = run_app(two_searches, steps, agent=agent)
    assert count == 1 and not asked
    assert bar.endswith(f"Frank Herbert - Dune (Retail) {note}")


def test_d_dismisses_a_finished_book_without_asking(two_searches):
    agent = FakeAgent()

    async def steps(app, pilot):
        book(agent, MESSIAH_LINE, created_at=1000.0, state=S.FAILED, error="No DCC offer")
        book(agent, created_at=2000.0, state=S.DONE, path=Path("/d/Dune.epub"), hook=H.DONE)
        await pilot.press("k", "k", "d")  # the newer one, at the top
        await pilot.pause()
        after_one = (headings_and_entries(app)[:3], highlighted(app),
                     app.query_one(ConfirmPrompt).display)
        await pilot.press("d")
        await pilot.pause()
        return after_one, headings_and_entries(app), highlighted(app), app.shown.query

    (lines, high, asked), after, high_after, shown = run_app(two_searches, steps, agent=agent)
    assert lines == ["BOOKS", "✗ Frank Herbert - Dune Messiah | No DCC offer", "SEARCHES"]
    assert high == "✗ Frank Herbert - Dune Messiah" and not asked
    # The last one: the section goes, and the highlight moves on to the searches
    assert [l.split(" | ")[0] for l in after] == ["dune frank herbert", "warhammer"]
    assert high_after == shown == "dune frank herbert"
    assert Path("/d/Dune.epub").exists() is False  # nothing on disk was ever involved


def test_quitting_while_a_book_is_moving_asks_first(two_searches):
    agent = FakeAgent()

    async def steps(app, pilot):
        book(agent, state=S.DONE, path=Path("/d/Dune.epub"), hook=H.RUNNING)
        await pilot.pause()
        await pilot.press("q")
        await pilot.pause()
        return app.is_running, status(app)

    running, bar = run_app(two_searches, steps, agent=agent)
    assert running and bar.endswith("1 book still moving: press q again to quit")


def test_quitting_says_what_it_waits_for(two_searches):
    agent = FakeAgent()
    seen = []
    stop = agent.stop

    async def slow_stop():
        seen.append(status(app_ref[0]))  # what the bar says while stop() runs
        await stop()
    agent.stop = slow_stop
    app_ref = []

    async def steps(app, pilot):
        app_ref.append(app)
        book(agent, state=S.DONE, path=Path("/d/Dune.epub"), hook=H.RUNNING)
        await pilot.pause()
        await pilot.press("q", "q")
        await pilot.pause()

    run_app(two_searches, steps, agent=agent)
    assert seen == ["waiting for 1 book to finish moving, then disconnecting…"]


def test_both_kinds_of_work_in_the_quit_question(two_searches):
    agent = FakeAgent()

    async def steps(app, pilot):
        book(agent, state=S.DONE, path=Path("/d/Dune.epub"), hook=H.RUNNING)
        book(agent, MESSIAH_LINE, state=S.WAITING)
        await pilot.pause()
        await pilot.press("q")
        return status(app)

    assert run_app(two_searches, steps, agent=agent).endswith(
        "1 request in progress, 1 book still moving: press q again to quit and cancel")


# --- the log ----------------------------------------------------------------------

def log_lines(app):
    """What the log view shows, line by line."""
    return [strip.text.rstrip() for strip in app.query_one(LogView).lines]


def test_tab_switches_to_the_log_and_back(two_searches):
    async def steps(app, pilot):
        await pilot.press("enter")  # into the results first
        await pilot.press("tab")
        log_view = app.query_one(LogView)
        opened = (log_view.display, app.query_one("#main").display, app.focused is log_view)
        await pilot.press("tab")
        return opened, log_view.display, app.query_one("#main").display, app.focused

    (log_shown, main_shown, focused), log_after, main_after, back = run_app(two_searches, steps)
    assert log_shown and not main_shown and focused  # full screen
    assert not log_after and main_after
    assert isinstance(back, ResultsTable)  # where it was


def test_esc_also_leaves_the_log(two_searches):
    async def steps(app, pilot):
        await pilot.press("tab", "escape")
        return app.query_one(LogView).display, app.focused

    log_shown, focused = run_app(two_searches, steps)
    assert not log_shown and isinstance(focused, SearchList)


def test_the_log_shows_what_the_log_file_gets(two_searches):
    async def steps(app, pilot):
        log = logging.getLogger("server.agent")
        log.info("Connected to irc.example.net, joining #ebooks")
        log.warning("Hook exited with status 23 for Dune.epub:\nrsync: connection refused")
        try:
            raise ValueError("boom")
        except ValueError:
            log.exception("Request failed unexpectedly")
        logging.getLogger("server.dcc").debug("not at INFO: not shown")
        await pilot.press("tab")
        await pilot.pause()
        return log_lines(app)

    lines = run_app(two_searches, steps)
    text = "\n".join(lines)
    assert re.search(r"^\d\d:\d\d:\d\d INFO  Connected to irc.example.net, joining #ebooks$",
                     text, re.M)
    # Continuation lines line up under the message, not the time
    indent = " " * len("20:14:03 WARN  ")
    assert re.search(r"^\d\d:\d\d:\d\d WARN  Hook exited with status 23 for Dune.epub:$", text, re.M)
    assert indent + "rsync: connection refused" in lines
    assert re.search(r"ERROR Request failed unexpectedly\n {15}Traceback", text)
    assert indent + "ValueError: boom" in lines
    assert "not shown" not in text


def test_levels_are_coloured():
    st = Styles.for_theme(BUILTIN_THEMES["ansi-dark"])

    def record(level):
        return logging.LogRecord("x", level, __file__, 1, "msg", None, None)

    def styles(level):
        _, level_text, message = record_parts(record(level), st)
        return level_text.style, message.style

    assert styles(logging.WARNING) == (st.warning, st.warning)
    assert styles(logging.ERROR) == (st.failed, st.failed)
    assert styles(logging.INFO) == (st.muted, "")


# --- theming ----------------------------------------------------------------------

@pytest.mark.parametrize("theme, failed, warning", [
    # The terminal's own red and yellow: follows the terminal's theme
    ("ansi-dark", "color(1)", "color(3)"),
    # The theme's colours for text, not its background ones
    ("tokyo-night", "#f9a4b4", "#eaca9b"),
])
def test_roles_follow_the_theme(theme, failed, warning):
    st = Styles.for_theme(BUILTIN_THEMES[theme])
    assert (str(st.failed), str(st.warning)) == (failed, warning)
    assert (str(st.muted), str(st.strong), str(st.heading)) == ("dim", "bold", "bold dim")


def test_the_app_uses_the_theme_its_given(two_searches):
    async def go():
        app = EbooksApp(FakeAgent(), two_searches, theme="tokyo-night")
        async with app.run_test() as pilot:
            await pilot.pause()
            return app.theme, str(app.text_styles.failed)

    assert asyncio.run(go()) == ("tokyo-night", "#f9a4b4")


def test_ansi_dark_by_default(two_searches):
    async def steps(app, pilot):
        return app.theme

    assert run_app(two_searches, steps) == "ansi-dark"


def test_a_failed_book_marker_uses_the_failed_style(two_searches):
    agent = FakeAgent()

    async def steps(app, pilot):
        book(agent, state=S.FAILED, error="No DCC offer")
        await pilot.pause()
        prompt = app.query_one(SearchList).get_option_at_index(1).prompt
        return [(prompt.plain[s.start:s.end], s.style) for s in prompt.spans], app.text_styles

    spans, st = run_app(two_searches, steps, agent=agent)
    assert ("✗", st.failed + st.strong) in spans
    assert ("No DCC offer", st.muted) in spans  # not the failed colour: unreadable on the highlight


def test_records_from_other_threads_arrive(two_searches):
    async def steps(app, pilot):
        # e.g. zip extraction, which runs in a thread
        thread = threading.Thread(
            target=lambda: logging.getLogger("server.dcc").info("Extracted Dune.epub"))
        thread.start()
        thread.join()
        await pilot.press("tab")
        await pilot.pause()
        return log_lines(app)

    assert any(line.endswith("INFO  Extracted Dune.epub") for line in run_app(two_searches, steps))


def test_the_log_follows_new_lines_only_at_the_bottom(two_searches):
    log = logging.getLogger("server.agent")

    async def steps(app, pilot):
        view = app.query_one(LogView)
        await pilot.press("tab")
        for i in range(100):
            log.info("line %d", i)
        await pilot.pause()
        following = view.is_vertical_scroll_end
        await pilot.press("k", "k", "k")
        await pilot.pause()
        scrolled_to = view.scroll_y
        log.info("while reading")
        await pilot.pause()
        stayed = view.scroll_y == scrolled_to
        await pilot.press("G")
        log.info("after G")
        await pilot.pause()
        return following, stayed, view.is_vertical_scroll_end, log_lines(app)[-1]

    following, stayed, back_at_end, last = run_app(two_searches, steps)
    assert following
    assert stayed  # reading further up isn't interrupted
    assert back_at_end and last.endswith("after G")


def test_gg_goes_to_the_top(two_searches):
    log = logging.getLogger("server.agent")

    async def steps(app, pilot):
        view = app.query_one(LogView)
        await pilot.press("tab")
        for i in range(100):
            log.info("line %d", i)
        await pilot.pause()
        await pilot.press("g", "k", "g")  # not gg: something in between
        await pilot.pause()
        not_gg = view.scroll_y
        await pilot.press("g", "g")
        await pilot.pause()
        return not_gg, view.scroll_y

    not_gg, after_gg = run_app(two_searches, steps)
    assert not_gg > 0 and after_gg == 0


def test_results_arriving_while_the_log_is_open_dont_close_it(two_searches):
    agent = FakeAgent()

    async def steps(app, pilot):
        await pilot.press("s", *"ac/dc", "enter")
        await pilot.press("tab")
        finish_search(agent, agent.requests()[0], two_searches)
        await pilot.pause()
        reading = (app.query_one(LogView).display, isinstance(app.focused, LogView))
        await pilot.press("tab")
        return reading, app.focused, app.shown.query

    (log_shown, in_log), focused, shown = run_app(two_searches, steps, agent=agent)
    assert log_shown and in_log
    assert isinstance(focused, ResultsTable) and shown == "ac/dc"  # they're waiting for you


def test_searching_from_the_log_comes_back_to_it(two_searches):
    async def steps(app, pilot):
        await pilot.press("tab", "s", *"dune", "enter")
        await pilot.pause()
        return app.focused, app.query_one(LogView).display

    focused, log_shown = run_app(two_searches, steps)
    assert isinstance(focused, LogView) and log_shown


def test_the_handler_goes_when_the_app_does(two_searches):
    root = logging.getLogger()
    level, handlers = root.level, list(root.handlers)

    async def steps(app, pilot):
        return root.getEffectiveLevel()

    during = run_app(two_searches, steps)
    assert during == logging.INFO
    assert root.level == level and root.handlers == handlers


def test_an_unknown_theme_is_a_config_error():
    import ebooks_tui
    from config import ConfigError
    ebooks_tui.check_theme("gruvbox")  # fine
    with pytest.raises(ConfigError, match=r"tui.theme: unknown theme 'gruvbx' \(one of: .*gruvbox"):
        ebooks_tui.check_theme("gruvbx")


def test_the_highlighted_entry_is_drawn_plain(two_searches):
    # Its dim and coloured text would be unreadable on the highlight
    agent = FakeAgent()

    def spans(app, index):
        return app.query_one(SearchList).get_option_at_index(index).prompt.spans

    async def steps(app, pilot):
        book(agent, state=S.FAILED, error="No DCC offer")
        await pilot.pause()
        before = (spans(app, 1), spans(app, 3))  # the book; the highlighted search
        await pilot.press("k")  # onto the book
        after = (spans(app, 1), spans(app, 3))
        agent.update(agent.requests()[0].id, error="still no offer")  # while highlighted
        await pilot.pause()
        return before, after, spans(app, 1), sidebar(app)[1]

    (book_before, search_before), (book_after, search_after), updated, entry = run_app(
        two_searches, steps, agent=agent)
    assert book_before and not search_before
    assert not book_after and search_after  # the plain one moved with the highlight
    # An update to it while highlighted stays plain, with the new text
    assert not updated and entry == ("✗ Frank Herbert - Dune (Retail)", "still no offer")


@pytest.mark.parametrize("theme, alert, on_alert", [
    # ANSI: solid yellow, the terminal's black on it (not its light text)
    ("ansi-dark", "ansi_yellow", "ansi_black"),
    # Others: the theme's warning, and whichever of black or white reads best on it
    ("tokyo-night", "#DFAF68", "auto 90%"),
])
def test_the_yn_bar_has_readable_colours(theme, alert, on_alert):
    from tui.styles import css_variables
    assert css_variables(BUILTIN_THEMES[theme]) == {"alert": alert, "on-alert": on_alert}


def test_the_yn_bar_uses_them(two_searches):
    async def steps(app, pilot):
        await pilot.press("d")
        prompt = app.query_one(ConfirmPrompt)
        return prompt.styles.background, prompt.styles.color

    background, color = run_app(two_searches, steps)
    assert background.ansi is not None and color.ansi is not None  # terminal colours, by default
