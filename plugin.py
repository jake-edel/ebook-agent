"""irc3 plugin — handles IRC events and drives DCC downloads."""

import asyncio
import logging
import socket
import struct
from pathlib import Path

import irc3

from dcc import DOWNLOADS_DIR, DCC_SEND_RE, extract_ebook, receive_dcc

log = logging.getLogger(__name__)


@irc3.plugin
class EbooksPlugin:
    def __init__(self, bot):
        self.bot = bot
        self._dcc_queue: asyncio.Queue = asyncio.Queue()
        self._connected = asyncio.Event()

    def on_connected(self, **kwargs):
        self.bot.join("#ebooks")
        self._connected.set()
        print(f"Connected to {self.bot.config.host} and joined #ebooks")

    def on_dcc_send(self, mask=None, target=None, text=None, **kwargs):
        m = DCC_SEND_RE.search(text)
        if not m:
            return
        filename, ip_int, port, filesize = m.group(1), int(m.group(2)), int(m.group(3)), int(m.group(4))
        ip = socket.inet_ntoa(struct.pack("!I", ip_int))
        self._dcc_queue.put_nowait((filename, ip, port, filesize))

    async def search(self, term: str, timeout: int = 60) -> Path:
        self.bot.privmsg("#ebooks", f"@search {term}")
        log.info("Search submitted: %s", term)

        try:
            filename, ip, port, filesize = await asyncio.wait_for(
                self._dcc_queue.get(), timeout=timeout
            )
        except asyncio.TimeoutError:
            raise TimeoutError(f"No DCC offer received within {timeout}s for '{term}'")

        dest = DOWNLOADS_DIR / filename
        log.info("DCC SEND offer: %s from %s:%s (%s bytes)", filename, ip, port, filesize)
        await receive_dcc(ip, port, filesize, dest)
        log.info("Download complete: %s", dest)
        if dest.suffix.lower() == ".zip":
            dest = extract_ebook(dest, term)
            log.info("Extracted: %s", dest)
        return dest
