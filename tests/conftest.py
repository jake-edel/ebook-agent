"""Shared fixtures and helpers for the test suite."""

import asyncio
import ipaddress
import zipfile

import pytest
from irc3.utils import IrcString

import dcc
import plugin

# A real public address (example.com). Offers must point at public IPs, but
# tests never actually connect to it — downloads are faked or use 127.0.0.1.
PUBLIC_IP = "93.184.216.34"
OUR_NICK = "ebooks_cli"


def ip_int(ip: str) -> int:
    """DCC encodes IPv4 addresses as a single integer."""
    return int(ipaddress.IPv4Address(ip))


def offer_text(name="book.epub", ip=PUBLIC_IP, port=5000, size=10, ctcp=True):
    body = f'DCC SEND "{name}" {ip_int(ip)} {port} {size}'
    return f"\x01{body}\x01" if ctcp else body


def mask(nick: str) -> IrcString:
    return IrcString(f"{nick}!user@host.example")


@pytest.fixture(autouse=True)
def downloads(tmp_path, monkeypatch):
    """Point DOWNLOADS_DIR at a fresh temp dir so tests never touch real downloads."""
    d = (tmp_path / "downloads").resolve()
    d.mkdir()
    monkeypatch.setattr(dcc, "DOWNLOADS_DIR", d)
    return d


@pytest.fixture(autouse=True)
def searches(tmp_path, monkeypatch):
    """Same for SEARCHES_DIR, where results files go."""
    d = (tmp_path / "searches").resolve()
    d.mkdir()
    monkeypatch.setattr(dcc, "SEARCHES_DIR", d)
    return d


@pytest.fixture
def outside(tmp_path):
    """A directory next to downloads/ that nothing should ever be written into."""
    d = tmp_path / "outside"
    d.mkdir()
    return d


@pytest.fixture
def make_zip(downloads):
    def _make(name: str, payload: bytes, member="book.epub"):
        path = downloads / name
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr(member, payload)
        return path
    return _make


class FakeBot:
    """Just enough of irc3.IrcBot for EbooksPlugin to work against."""

    def __init__(self, members=("Search", "Oatmeal")):
        self.nick = OUR_NICK
        self.channels = {plugin.CHANNEL: set(members) | {OUR_NICK}}
        self.sent = []

    def privmsg(self, target, message):
        self.sent.append((target, message))


@pytest.fixture
def received(monkeypatch):
    """Replace the real DCC transfer with a stub that records calls and writes a byte."""
    calls = []

    async def fake_receive(ip, port, size, dest):
        calls.append((ip, port, size, dest))
        dest.write_bytes(b"x")
        return dest

    monkeypatch.setattr(plugin, "receive_dcc", fake_receive)
    return calls


@pytest.fixture
def ebooks_plugin(received):
    return plugin.EbooksPlugin(FakeBot())


async def local_sender(payload: bytes, hang: bool = False):
    """Start a TCP server on 127.0.0.1 that plays the DCC sender role.

    Returns (server, port). Sends `payload`, then optionally stalls.
    """
    async def handle(reader, writer):
        writer.write(payload)
        await writer.drain()
        if hang:
            await asyncio.sleep(10)
        try:
            await asyncio.wait_for(reader.read(), 1)  # swallow our acks
        except (asyncio.TimeoutError, ConnectionError):
            pass
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, server.sockets[0].getsockname()[1]
