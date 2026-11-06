"""Who may send us offers, and when — the plugin's gating logic."""

import asyncio

import pytest

import plugin
from agent import EbooksAgent
from dcc import MAX_DOWNLOAD_BYTES, FileTooLargeError, InvalidOfferError

from conftest import OUR_NICK, PUBLIC_IP, ip_int, mask, offer_text


async def search_with(p, term, deliveries, wait=0.3):
    """Start a search, deliver (sender, target, text) offers, return the result.

    Returns None if the search was still waiting after `wait` seconds
    (i.e. every offer was ignored). Exceptions from the search propagate.
    """
    task = asyncio.create_task(p.search(term))
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
    assert ebooks_plugin._pending is None  # slot cleared after cancel


def test_unsolicited_offer_dropped_and_not_used_later(ebooks_plugin, received):
    async def go():
        ebooks_plugin.on_dcc_send(mask=mask("Oatmeal"), target=OUR_NICK, text=offer_text("stale.epub"))
        assert ebooks_plugin._pending is None
        # The next search must not be satisfied by the stale offer
        assert await search_with(ebooks_plugin, "dune", []) is None
    run(go())


def test_late_offer_after_search_dropped(ebooks_plugin, received):
    async def go():
        res = await search_with(ebooks_plugin, "dune", [("Oatmeal", OUR_NICK, offer_text())])
        assert res is not None
        ebooks_plugin.on_dcc_send(mask=mask("Oatmeal"), target=OUR_NICK, text=offer_text("late.epub"))
        assert ebooks_plugin._pending is None
    run(go())


# --- offers that must be accepted -----------------------------------------

def test_member_offer_to_our_nick_accepted(ebooks_plugin, received):
    res = run(search_with(ebooks_plugin, "dune", [("Oatmeal", OUR_NICK, offer_text())]))
    assert res.name == "book.epub"
    assert received[0][:3] == (PUBLIC_IP, 5000, 10)
    assert ebooks_plugin.bot.sent == [("#ebooks", "dune")]


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
        ("Oatmeal", OUR_NICK, offer_text("first.epub")),
        ("Search", OUR_NICK, offer_text("second.epub")),
    ]))
    assert res.name == "first.epub"
    assert len(received) == 1


def test_concurrent_searches_each_get_their_own_offer(ebooks_plugin, received):
    async def go():
        t1 = asyncio.create_task(ebooks_plugin.search("one"))
        t2 = asyncio.create_task(ebooks_plugin.search("two"))
        await asyncio.sleep(0.01)
        ebooks_plugin.on_dcc_send(mask=mask("Oatmeal"), target=OUR_NICK, text=offer_text("one.epub"))
        await asyncio.sleep(0.01)
        ebooks_plugin.on_dcc_send(mask=mask("Oatmeal"), target=OUR_NICK, text=offer_text("two.epub"))
        return await asyncio.gather(t1, t2)

    r1, r2 = run(go())
    assert (r1.name, r2.name) == ("one.epub", "two.epub")


# --- bad offers from an eligible sender fail fast -------------------------

@pytest.mark.parametrize("text, error", [
    (offer_text(size=MAX_DOWNLOAD_BYTES * 5), FileTooLargeError),
    (offer_text(ip="127.0.0.1"), InvalidOfferError),
])
def test_bad_offer_from_eligible_sender_fails_search_immediately(ebooks_plugin, received, text, error):
    with pytest.raises(error):
        run(search_with(ebooks_plugin, "dune", [("Search", OUR_NICK, text)]))
    assert not received
    assert ebooks_plugin._pending is None


# --- end to end through irc3's real line dispatch -------------------------

@pytest.mark.parametrize("term, line, accepted", [
    ("@search dune",
     f':Search!Search@irchighway.net PRIVMSG {OUR_NICK} :{offer_text("results.txt.zip")}', True),
    ("!Oatmeal Dune.epub",
     f':Oatmeal!o@host.net PRIVMSG {OUR_NICK} :{offer_text("Dune.epub")}', True),
    ("@search dune",  # IRCv3 message tags before the prefix
     f'@time=2026-11-06T12:00:00Z :Search!s@h.net PRIVMSG {OUR_NICK} :{offer_text("x.zip", ctcp=False)}', True),
    ("@search dune",
     f':Mallory!m@evil.net PRIVMSG {OUR_NICK} :{offer_text()}', False),
    ("@search dune",  # sent to the channel
     f':Search!s@h.net PRIVMSG #ebooks :{offer_text()}', False),
])
def test_raw_irc_lines(received, monkeypatch, term, line, accepted):
    # Fake zips aren't real archives; skip extraction
    monkeypatch.setattr(plugin, "extract_ebook", lambda dest, term: dest)

    async def go():
        bot = EbooksAgent(nick=OUR_NICK)._build_bot()
        bot.privmsg = lambda *a, **k: None
        p = bot.get_plugin(plugin.EbooksPlugin)
        # Server's NAMES reply populates the channel member list
        bot.dispatch(f":irc.irchighway.net 353 {OUR_NICK} = #ebooks :{OUR_NICK} +Search @Oatmeal someone")
        await asyncio.sleep(0)

        task = asyncio.create_task(p.search(term))
        await asyncio.sleep(0.01)
        bot.dispatch(line)
        try:
            return await asyncio.wait_for(task, 0.3)
        except asyncio.TimeoutError:
            return None

    res = run(go())
    assert (res is not None) == accepted
