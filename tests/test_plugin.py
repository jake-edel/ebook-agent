"""Who may send us offers, and when — the plugin's gating logic."""

import asyncio

import pytest

import plugin
from agent import EbooksAgent
from config import Config, IrcConfig
from dcc import FileTooLargeError, InvalidOfferError

from conftest import OUR_NICK, PUBLIC_IP, FakeBot, ip_int, make_plugin, mask, offer_text

MAX_DOWNLOAD_BYTES = Config().downloads.max_size_bytes


def command(p, line):
    """"!Bot ..." is a book request; anything else is a search query."""
    return p.request(*plugin.check_request(line)) if line.startswith("!") else p.search(line)


async def search_with(p, term, deliveries, wait=0.3):
    """Start a search or request, deliver (sender, target, text) offers, return the result.

    Returns None if it was still waiting after `wait` seconds (i.e. every
    offer was ignored). Exceptions from the command propagate.
    """
    task = asyncio.create_task(command(p, term))
    await asyncio.sleep(0.01)
    for sender, target, text in deliveries:
        p.on_dcc_send(mask=mask(sender), target=target, text=text)
    try:
        return await asyncio.wait_for(task, wait)
    except asyncio.TimeoutError:
        return None


def run(coro):
    return asyncio.run(coro)


# --- offers that must be ignored ------------------------------------------

@pytest.mark.parametrize("label, term, deliveries", [
    ("sent to the channel, not us", "dune", [("Oatmeal", "#ebooks", offer_text())]),
    ("sender not in #ebooks", "dune", [("Mallory", OUR_NICK, offer_text())]),
    ("!Bot request answered by a different bot", "!Oatmeal Dune.epub", [("Search", OUR_NICK, offer_text())]),
    ("oversized offer from outsider", "dune",
     [("Mallory", OUR_NICK, offer_text(size=MAX_DOWNLOAD_BYTES * 5))]),
])
def test_ignored_offers(ebooks_plugin, received, label, term, deliveries):
    async def go():
        res = await search_with(ebooks_plugin, term, deliveries)
        assert res is None, label
        assert not received
    run(go())
    assert ebooks_plugin._waiters == []  # cleared after cancel


def test_unsolicited_offer_dropped_and_not_used_later(ebooks_plugin, received):
    async def go():
        ebooks_plugin.on_dcc_send(mask=mask("Oatmeal"), target=OUR_NICK, text=offer_text("stale.epub"))
        assert ebooks_plugin._waiters == []
        # The next search must not be satisfied by the stale offer
        assert await search_with(ebooks_plugin, "dune", []) is None
    run(go())


def test_late_offer_after_search_dropped(ebooks_plugin, received):
    async def go():
        res = await search_with(ebooks_plugin, "dune", [("Oatmeal", OUR_NICK, offer_text())])
        assert res is not None
        ebooks_plugin.on_dcc_send(mask=mask("Oatmeal"), target=OUR_NICK, text=offer_text("late.epub"))
        assert ebooks_plugin._waiters == []
    run(go())


# --- offers that must be accepted -----------------------------------------

def test_member_offer_to_our_nick_accepted(ebooks_plugin, received, searches):
    res = run(search_with(ebooks_plugin, "dune", [("Oatmeal", OUR_NICK, offer_text())]))
    assert res.parent == searches
    assert received[0][:3] == (PUBLIC_IP, 5000, 10)
    assert ebooks_plugin.bot.sent == [("#ebooks", "@search dune")]


def test_nick_matching_is_case_insensitive(ebooks_plugin, received):
    res = run(search_with(ebooks_plugin, "dune", [("oatmeal", OUR_NICK.upper(), offer_text())]))
    assert res is not None


def test_bang_request_only_accepts_named_bot(ebooks_plugin, received):
    res = run(search_with(ebooks_plugin, "!Oatmeal Dune.epub", [
        ("Search", OUR_NICK, offer_text("evil.epub")),
        ("Oatmeal", OUR_NICK, offer_text("Dune.epub")),
    ]))
    assert res.name == "Dune.epub"


def test_first_valid_offer_wins(ebooks_plugin, received):
    res = run(search_with(ebooks_plugin, "dune", [
        ("Oatmeal", OUR_NICK, offer_text(port=5001)),
        ("Search", OUR_NICK, offer_text(port=5002)),
    ]))
    assert res is not None
    assert [port for _, port, _, _ in received] == [5001]


def offer_from(p, nick, name, port=5000):
    p.on_dcc_send(mask=mask(nick), target=OUR_NICK, text=offer_text(name, port=port))


def test_offer_goes_to_the_request_naming_that_bot(ebooks_plugin, received):
    async def go():
        search = asyncio.create_task(ebooks_plugin.search("one"))
        book = asyncio.create_task(ebooks_plugin.request("!Oatmeal two.epub", "Oatmeal"))
        await asyncio.sleep(0.01)
        # Oatmeal's offer goes to the request naming it, even though the
        # search is also open and was sent first
        offer_from(ebooks_plugin, "Oatmeal", "two.epub")
        offer_from(ebooks_plugin, "Search", "one.txt")
        return await asyncio.gather(search, book)

    r1, r2 = run(go())
    assert r1.name.endswith(" one.txt")
    assert r2.name == "two.epub"


def test_requests_to_one_bot_are_matched_in_order(ebooks_plugin, received):
    async def go():
        a = asyncio.create_task(ebooks_plugin.request("!Oatmeal a.epub", "Oatmeal"))
        await asyncio.sleep(0.01)
        b = asyncio.create_task(ebooks_plugin.request("!Oatmeal b.epub", "Oatmeal"))
        await asyncio.sleep(0.01)
        offer_from(ebooks_plugin, "Oatmeal", "first.epub", port=5001)
        offer_from(ebooks_plugin, "Oatmeal", "second.epub", port=5002)
        return await asyncio.gather(a, b)

    a, b = run(go())
    assert (a.name, b.name) == ("first.epub", "second.epub")


def test_requests_to_different_bots_run_in_parallel(dl, received):
    p = make_plugin(dl, FakeBot(members=("Search", "Oatmeal", "Bsk")))

    async def go():
        slow = asyncio.create_task(p.request("!Oatmeal slow.epub", "Oatmeal"))
        fast = asyncio.create_task(p.request("!Bsk fast.epub", "Bsk"))
        await asyncio.sleep(0.01)
        # Both lines went out at once; Bsk answers while Oatmeal is silent
        assert [line for _, line in p.bot.sent] == ["!Oatmeal slow.epub", "!Bsk fast.epub"]
        offer_from(p, "Bsk", "fast.epub")
        path = await asyncio.wait_for(fast, 1)
        slow.cancel()
        return path

    assert run(go()).name == "fast.epub"


def test_second_search_waits_for_the_first(ebooks_plugin, received):
    async def go():
        a = asyncio.create_task(ebooks_plugin.search("one"))
        b = asyncio.create_task(ebooks_plugin.search("two"))
        await asyncio.sleep(0.01)
        assert ebooks_plugin.bot.sent == [("#ebooks", "@search one")]
        offer_from(ebooks_plugin, "Search", "one.txt")
        await asyncio.wait_for(a, 1)
        await asyncio.sleep(0.01)
        assert ebooks_plugin.bot.sent[-1] == ("#ebooks", "@search two")
        b.cancel()

    run(go())


# --- connection state ---------------------------------------------------------

def test_nothing_sent_until_ready(ebooks_plugin, received):
    async def go():
        ebooks_plugin.ready.clear()
        task = asyncio.create_task(ebooks_plugin.search("dune"))
        await asyncio.sleep(0.01)
        assert ebooks_plugin.bot.sent == []
        ebooks_plugin.on_names_end(channel="#ebooks")
        await asyncio.sleep(0.01)
        assert ebooks_plugin.bot.sent == [("#ebooks", "@search dune")]
        task.cancel()

    run(go())


def test_disconnect_fails_requests_waiting_for_an_offer(ebooks_plugin, received):
    async def go():
        task = asyncio.create_task(ebooks_plugin.request("!Oatmeal Dune.epub", "Oatmeal"))
        await asyncio.sleep(0.01)
        ebooks_plugin.connection_lost()
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(task, 1)
        assert not ebooks_plugin.ready.is_set()

    run(go())


def test_ready_only_for_our_channel(ebooks_plugin):
    statuses = []
    ebooks_plugin.on_status = lambda connected, reason: statuses.append(connected)
    ebooks_plugin.ready.clear()
    ebooks_plugin.on_names_end(channel="#other")
    assert not ebooks_plugin.ready.is_set()
    ebooks_plugin.on_names_end(channel="#EBOOKS")
    assert ebooks_plugin.ready.is_set()
    ebooks_plugin.connection_lost()
    assert statuses == [True, False]


# --- bad offers from an eligible sender fail fast -------------------------

@pytest.mark.parametrize("text, error", [
    (offer_text(size=MAX_DOWNLOAD_BYTES * 5), FileTooLargeError),
    (offer_text(ip="127.0.0.1"), InvalidOfferError),
    ("\x01DCC SEND garbled\x01", InvalidOfferError),
])
def test_bad_offer_from_eligible_sender_fails_search_immediately(ebooks_plugin, received, text, error):
    with pytest.raises(error):
        run(search_with(ebooks_plugin, "dune", [("Search", OUR_NICK, text)]))
    assert not received
    assert ebooks_plugin._waiters == []


# --- end to end through irc3's real line dispatch -------------------------

@pytest.mark.parametrize("term, line, accepted", [
    ("dune",
     f':Search!Search@irchighway.net PRIVMSG {OUR_NICK} :{offer_text("results.txt.zip")}', True),
    ("!Oatmeal Dune.epub",
     f':Oatmeal!o@host.net PRIVMSG {OUR_NICK} :{offer_text("Dune.epub")}', True),
    ("dune",  # IRCv3 message tags before the prefix
     f'@time=2026-11-06T12:00:00Z :Search!s@h.net PRIVMSG {OUR_NICK} :{offer_text("x.zip", ctcp=False)}', True),
    ("dune",
     f':Mallory!m@evil.net PRIVMSG {OUR_NICK} :{offer_text()}', False),
    ("dune",  # sent to the channel
     f':Search!s@h.net PRIVMSG #ebooks :{offer_text()}', False),
])
def test_raw_irc_lines(dl, received, monkeypatch, term, line, accepted):
    # Fake zips aren't real archives; skip extraction
    monkeypatch.setattr(dl, "extract", lambda path, name=None: path)

    async def go():
        bot = EbooksAgent(Config(irc=IrcConfig(nick=OUR_NICK)))._build_bot()
        bot.privmsg = lambda *a, **k: None
        p = bot.get_plugin(plugin.EbooksPlugin)
        p.downloads = dl  # whose receive() the `received` fixture stubbed
        # Server's NAMES reply populates the channel member list
        bot.dispatch(f":irc.irchighway.net 353 {OUR_NICK} = #ebooks :{OUR_NICK} +Search @Oatmeal someone")
        bot.dispatch(f":irc.irchighway.net 366 {OUR_NICK} #ebooks :End of /NAMES list.")
        await asyncio.sleep(0)

        task = asyncio.create_task(command(p, term))
        await asyncio.sleep(0.01)
        bot.dispatch(line)
        try:
            return await asyncio.wait_for(task, 0.3)
        except asyncio.TimeoutError:
            return None

    res = run(go())
    assert (res is not None) == accepted


# --- message logging --------------------------------------------------------

@pytest.mark.parametrize("line, expected", [
    (f':Search!s@h.net PRIVMSG {OUR_NICK} :{offer_text("results.txt.zip")}',
     f"PRIVMSG from Search!s@h.net to {OUR_NICK}: {offer_text('results.txt.zip')!r}"),
    (f':Mallory!m@evil.net PRIVMSG {OUR_NICK} :hey, you a bot?',
     f"PRIVMSG from Mallory!m@evil.net to {OUR_NICK}: 'hey, you a bot?'"),
    (f':Oatmeal!o@host.net NOTICE {OUR_NICK} :You are in queue position 2',
     f"NOTICE from Oatmeal!o@host.net to {OUR_NICK}: 'You are in queue position 2'"),
    (f'@time=2026-11-06T12:00:00Z :irc.irchighway.net NOTICE {OUR_NICK} :Welcome',
     f"NOTICE from irc.irchighway.net to {OUR_NICK}: 'Welcome'"),
    ("NOTICE * :*** Looking up your hostname", "NOTICE from server to *: '*** Looking up your hostname'"),
    (':Search!s@h.net PRIVMSG #ebooks :channel chatter', None),
])
def test_messages_to_us_logged_verbatim(caplog, line, expected):
    async def go():
        bot = EbooksAgent(Config(irc=IrcConfig(nick=OUR_NICK)))._build_bot()
        bot.dispatch(line)
        await asyncio.sleep(0)  # irc3 runs handlers via call_soon

    with caplog.at_level("INFO", logger="plugin"):
        run(go())
    # Only the verbatim lines; the DCC handler may log its own verdict too
    logged = [r.getMessage() for r in caplog.records
              if r.name == "plugin" and r.getMessage().startswith(("PRIVMSG", "NOTICE"))]
    assert logged == ([expected] if expected else [])


# --- logging private messages ---------------------------------------------

@pytest.mark.parametrize("show, logged", [(True, True), (False, False)])
def test_show_private_messages(ebooks_plugin, caplog, show, logged):
    ebooks_plugin.show_private_messages = show
    with caplog.at_level("INFO", logger="plugin"):
        ebooks_plugin.on_message(mask=mask("Search"), command="NOTICE",
                                 target=OUR_NICK, text="hello there")
    assert ("hello there" in caplog.text) is logged


def test_hidden_private_messages_still_passed_on(ebooks_plugin):
    ebooks_plugin.show_private_messages = False
    got = []
    ebooks_plugin.on_bot_message = lambda nick, text, key: got.append((nick, text, key))
    ebooks_plugin.on_message(mask=mask("Search"), command="NOTICE",
                             target=OUR_NICK, text="hello there")
    assert got == [("Search", "hello there", None)]


def test_bot_messages_get_the_key_of_the_request_offers_would_go_to(ebooks_plugin):
    got = []
    ebooks_plugin.on_bot_message = lambda nick, text, key: got.append((nick, key))

    async def go():
        search = asyncio.create_task(ebooks_plugin.search("dune", key="s"))
        book = asyncio.create_task(ebooks_plugin.request("!Oatmeal Dune.epub", "Oatmeal", key="b"))
        await asyncio.sleep(0.01)
        for nick in ("Oatmeal", "OATMEAL", "Search", "Mallory"):
            ebooks_plugin.on_message(mask=mask(nick), command="NOTICE", target=OUR_NICK, text="hi")
        # Once Oatmeal's offer is in, its messages no longer belong to that request
        offer_from(ebooks_plugin, "Oatmeal", "Dune.epub")
        await asyncio.wait_for(book, 1)
        ebooks_plugin.on_message(mask=mask("Oatmeal"), command="NOTICE", target=OUR_NICK, text="hi")
        search.cancel()

    run(go())
    assert got == [("Oatmeal", "b"), ("OATMEAL", "b"), ("Search", "s"), ("Mallory", "s"),
                   ("Oatmeal", "s")]
