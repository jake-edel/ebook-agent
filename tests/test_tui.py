"""The TUI: browsing past searches on disk, and requesting books from them."""

import asyncio
import dataclasses
import itertools
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

import pytest

import plugin
from core import (
    AgentStatus,
    BotMessage,
    ConnectionState,
    Request,
    RequestKind,
    RequestState,
    RequestUpdated,
    StatusChanged,
)
from tui.app import DIVIDER, PREVIEW_DELAY, EbooksApp, ResultsTable, SearchList, human_size, marker
from tui.library import list_searches

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
        self._requests: dict[str, Request] = {}
        self._ids = itertools.count(1)
        self._queues: list[asyncio.Queue] = []
        self.stopped = False

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
                                bot=bot, state=S.PENDING, created_at=0, updated_at=0))

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
    return [table.get_row(r.key) for r in table.ordered_rows if r.key.value != DIVIDER]


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
    assert [(r[1], r[2], str(r[3]), r[4]) for r in shown] == [
        ("Bsk", "epub", "1.0M", "Frank Herbert - Dune (Retail)"),
        ("Oatmeal", "mobi", "681K", "Frank Herbert - Dune Messiah"),
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
    assert (query, count) == ("warhammer", 8)
    assert subtitle == "8 results · 14 lists · 2 not shown"
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
        return [(r[1], r[2]) for r in rows(app)], table.divider_row, str(divider[4])

    shown, divider_row, label = run_app(two_searches, steps)
    # The fixture lists two artemis_serv mobis first; each group keeps the bot's order
    assert shown == [("Bsk", "epub")] * 5 + [("artemis_serv", "mobi")] * 2 + [("Bsk", "mobi")]
    assert divider_row == 5
    assert label.startswith("── other formats (3) ──")


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
    # e.g. Neovim's :Ebook over the socket, which the app serves too
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
