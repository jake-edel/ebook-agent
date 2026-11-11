"""The TUI, stage 1: browsing past searches on disk."""

import asyncio
import os
import shutil
from datetime import datetime
from pathlib import Path

import pytest

from tui.app import PREVIEW_DELAY, EbooksApp, ResultsTable, SearchList, human_size
from tui.library import list_searches

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

def run_app(searches_dir, steps, size=(120, 30)):
    """Run the app headless, call `steps(app, pilot)`, and return what it returns."""
    async def go():
        app = EbooksApp(searches_dir)
        async with app.run_test(size=size) as pilot:
            await pilot.pause()
            return await steps(app, pilot)
    return asyncio.run(go())


def rows(app):
    table = app.query_one(ResultsTable)
    return [table.get_row_at(i) for i in range(table.row_count)]


async def settle(pilot):
    # Past the preview delay
    await pilot.pause(PREVIEW_DELAY + 0.1)


def test_opens_on_the_newest_search(two_searches):
    async def steps(app, pilot):
        return app.focused, rows(app), app.query_one("#status").render()

    focused, shown, status = run_app(two_searches, steps)
    assert isinstance(focused, SearchList)
    assert [(r[0], r[1], str(r[2]), r[3]) for r in shown] == [
        ("Bsk", "epub", "1.0M", "Frank Herbert - Dune (Retail)"),
        ("Oatmeal", "mobi", "681K", "Frank Herbert - Dune Messiah"),
    ]
    assert "offline" in str(status)


def test_moving_through_searches_previews_them(two_searches):
    async def steps(app, pilot):
        await pilot.press("j")
        await settle(pilot)
        table = app.query_one(ResultsTable)
        after_j = (app.shown.query, table.row_count, table.border_subtitle)
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
    assert cursor == 1
    assert isinstance(back, SearchList)


def test_enter_opens_the_highlighted_search_at_once(two_searches):
    async def steps(app, pilot):
        await pilot.press("j", "enter")  # no wait for the preview
        return app.shown.query, isinstance(app.focused, ResultsTable)

    assert run_app(two_searches, steps) == ("warhammer", True)


def test_enter_on_a_result_shows_its_request_line(two_searches):
    # Requesting books comes in stage 2
    async def steps(app, pilot):
        await pilot.press("enter", "j", "enter")
        return str(app.query_one("#status").render())

    assert "!Oatmeal Frank Herbert - Dune Messiah.mobi" in run_app(two_searches, steps)


def test_no_searches_yet(searches):
    async def steps(app, pilot):
        return rows(app), str(app.query_one("#status").render())

    shown, status = run_app(searches, steps)
    assert shown == []
    assert "no searches yet" in status


def test_q_quits(two_searches):
    async def steps(app, pilot):
        await pilot.press("enter")  # from the results pane too
        await pilot.press("q")
        await pilot.pause()
        return app.is_running

    assert run_app(two_searches, steps) is False
