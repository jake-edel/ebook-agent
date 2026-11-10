"""EbooksAgent as the core interface: commands return at once, events report the rest."""

import asyncio

import pytest

from agent import EbooksAgent
from config import Config, IrcConfig
from core import BotMessage, RequestKind, RequestState, StatusChanged

from conftest import OUR_NICK, make_plugin, mask, offer_text

S = RequestState


@pytest.fixture
def agent(dl, received):
    """An EbooksAgent around a test plugin (fake IRC, stubbed transfers), already joined."""
    a = EbooksAgent(Config(irc=IrcConfig(nick=OUR_NICK)))
    a._plugin = make_plugin(dl)
    a._attach(a._plugin)
    return a


def offer_from(agent, nick, name="book.epub", size=10):
    agent._plugin.on_dcc_send(mask=mask(nick), target=OUR_NICK, text=offer_text(name, size=size))


def record(agent):
    """Subscribe and collect events in the background; returns the list it fills."""
    seen = []
    stream = agent.events()

    async def consume():
        async for event in stream:
            seen.append(event)

    asyncio.create_task(consume())
    return seen


async def collect(stream, n):
    return [await asyncio.wait_for(anext(stream), 1) for _ in range(n)]


def test_search_returns_at_once_then_reports_each_step(agent, searches):
    async def go():
        events = record(agent)
        req = agent.search("dune")
        assert req.state is S.PENDING and req.kind is RequestKind.SEARCH
        assert req.line == "@search dune" and req.bot is None
        await asyncio.sleep(0.01)
        offer_from(agent, "Search", "r.txt")
        final = await agent.wait(req.id)
        await asyncio.sleep(0)
        return req, final, events

    req, final, events = asyncio.run(go())
    assert final.state is S.DONE and final.path.parent == searches
    # The stubbed transfer reports no progress
    assert [e.request.state for e in events] == [S.PENDING, S.WAITING, S.TRANSFERRING, S.DONE]
    transferring = next(e.request for e in events if e.request.state is S.TRANSFERRING)
    assert (transferring.filename, transferring.size) == ("r.txt", 10)
    assert agent.requests() == (final,)


def test_book_request_names_its_bot(agent, downloads):
    async def go():
        req = agent.request("!Oatmeal Dune.epub")
        await asyncio.sleep(0.01)
        offer_from(agent, "Oatmeal", "Dune.epub")
        return req, await agent.wait(req.id)

    req, final = asyncio.run(go())
    assert (req.kind, req.bot) == (RequestKind.BOOK, "Oatmeal")
    assert final.path == downloads / "Dune.epub"


def test_bad_input_raises_at_once_and_records_nothing(agent):
    async def go():
        with pytest.raises(ValueError):
            agent.search("dune\r\nQUIT")
        with pytest.raises(ValueError):
            agent.request("Dune.epub")

    asyncio.run(go())
    assert agent.requests() == ()
    assert agent._plugin.bot.sent == []


def test_commands_before_start_raise():
    with pytest.raises(RuntimeError, match="start"):
        EbooksAgent().search("dune")


def test_failure_is_a_failed_snapshot(agent):
    agent._plugin.offer_timeout = 0.05

    async def go():
        return await agent.wait(agent.search("dune").id)

    final = asyncio.run(go())
    assert final.state is S.FAILED
    assert "No DCC offer" in final.error


def test_unexpected_errors_fail_the_request_not_the_agent(agent, monkeypatch):
    async def broken(query, update):
        raise RuntimeError("boom")

    monkeypatch.setattr(agent._plugin, "search", broken)

    async def go():
        return await agent.wait(agent.search("dune").id)

    final = asyncio.run(go())
    assert final.state is S.FAILED and "internal error" in final.error


@pytest.mark.parametrize("delay", [0, 0.01])  # before it starts, and while it waits
def test_cancel(agent, delay):
    async def go():
        req = agent.request("!Oatmeal Dune.epub")
        await asyncio.sleep(delay)
        agent.cancel(req.id)
        final = await agent.wait(req.id)
        # A late offer finds nothing waiting
        offer_from(agent, "Oatmeal", "Dune.epub")
        await asyncio.sleep(0.01)
        return final

    final = asyncio.run(go())
    assert final.state is S.CANCELLED
    assert agent._plugin._waiters == []
    agent.cancel(final.id)  # finished: a no-op
    assert agent.requests()[0].state is S.CANCELLED


def test_unknown_id(agent):
    with pytest.raises(KeyError):
        agent.cancel("nope")
    with pytest.raises(KeyError):
        asyncio.run(agent.wait("nope"))


def test_progress_is_rate_limited(agent, dl, monkeypatch):
    async def fast_receive(ip, port, size, dest, on_progress=None):
        for n in range(1, size + 1):
            on_progress(n)
        dest.write_bytes(b"x" * size)
        return dest

    monkeypatch.setattr(dl, "receive", fast_receive)

    async def go():
        events = record(agent)
        req = agent.request("!Oatmeal Dune.epub")
        await asyncio.sleep(0.01)
        offer_from(agent, "Oatmeal", "Dune.epub", size=1000)
        await agent.wait(req.id)
        await asyncio.sleep(0)
        return [e.request for e in events]

    # 1000 progress calls in a burst: only the final byte count gets through
    snapshots = asyncio.run(go())
    received = [r.received for r in snapshots if r.state is S.TRANSFERRING and r.received]
    assert received == [1000]
    assert snapshots[-1].state is S.DONE


def test_each_stream_gets_every_event_and_can_unsubscribe(agent):
    async def go():
        a, b = agent.events(), agent.events()
        req = agent.search("dune")
        first_a, first_b = await anext(a), await anext(b)
        await a.aclose()
        assert len(agent._subscribers) == 1  # only b now
        agent.cancel(req.id)
        await agent.wait(req.id)
        return first_a, first_b, await anext(b)

    first_a, first_b, later = asyncio.run(go())
    assert first_a == first_b and first_a.request.state is S.PENDING
    assert later.request.state is S.CANCELLED


def test_status_and_status_events(agent):
    async def go():
        events = agent.events()
        assert agent.status().connected
        agent._plugin.connection_lost()
        assert not agent.status().connected
        agent._plugin.on_names_end(channel="#ebooks")
        return await collect(events, 2)

    down, up = asyncio.run(go())
    assert isinstance(down, StatusChanged) and not down.status.connected
    assert down.reason == "disconnected"
    assert up.status.connected and up.reason == "joined"
    assert up.status.nick == OUR_NICK and up.status.channel == "#ebooks"


def test_disconnect_fails_waiting_requests(agent):
    async def go():
        req = agent.request("!Oatmeal Dune.epub")
        await asyncio.sleep(0.01)
        agent._plugin.connection_lost()
        return await agent.wait(req.id)

    final = asyncio.run(go())
    assert final.state is S.FAILED and "Disconnected" in final.error


@pytest.mark.parametrize("nick, linked", [
    ("Oatmeal", "book"),     # the bot a request names
    ("oatmeal", "book"),
    ("Search", "search"),    # anyone else: the search in flight
])
def test_bot_messages_linked_to_requests(agent, nick, linked):
    async def go():
        book = agent.request("!Oatmeal Dune.epub")
        search = agent.search("dune")
        await asyncio.sleep(0.01)
        events = agent.events()
        agent._plugin.on_message(mask=mask(nick), command="NOTICE", target=OUR_NICK, text="hi")
        msg = await anext(events)
        for r in (book, search):
            agent.cancel(r.id)
        return msg, {"book": book.id, "search": search.id}[linked]

    msg, expected = asyncio.run(go())
    assert msg == BotMessage(nick, "hi", expected)


def test_bot_message_with_nothing_open_is_unlinked(agent):
    async def go():
        events = agent.events()
        agent._plugin.on_message(mask=mask("Mallory"), command="PRIVMSG", target=OUR_NICK, text="hey")
        # DCC offers are CTCP, not messages
        agent._plugin.on_message(mask=mask("Mallory"), command="PRIVMSG", target=OUR_NICK,
                                 text=offer_text())
        msg = await anext(events)
        assert agent._subscribers and all(q.empty() for q in agent._subscribers)
        return msg

    assert asyncio.run(go()) == BotMessage("Mallory", "hey", None)


def test_stop_cancels_outstanding_requests(agent):
    async def go():
        req = agent.request("!Oatmeal Dune.epub")
        await asyncio.sleep(0.01)
        await agent.stop()  # no IRC connection to close here
        return agent.requests()[0]

    final = asyncio.run(go())
    assert final.state is S.CANCELLED
    assert agent._tasks[final.id].done()
