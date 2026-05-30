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
    # **kwargs - pick up and discard any additional arguments
    # to prevent error from being thrown
    def on_connected(self, **kwargs):
        # Once connected, join the #ebooks channel
        self.bot.join("#ebooks")
        # Signal to the parent that we're done with the connected lifecycle
        self._connected.set()
        print(f"Connected to {self.bot.config.host} and joined #ebooks")

    def on_dcc_send(self, mask=None, target=None, text=None, **kwargs):
        # Is this a proper DCC message
        m = DCC_SEND_RE.search(text)
        if not m:
            return
        # Unpack the named groups — quoted handles filenames with spaces,
        # bare handles single-word filenames without quotes
        filename = m.group("quoted") or m.group("bare")
        ip_int = int(m.group("ip"))
        port = int(m.group("port"))
        filesize = int(m.group("filesize"))
        # Bit wizardry to convert the large int IP representation to a network IP address
        ip = socket.inet_ntoa(struct.pack("!I", ip_int))
        # Stuff into the file queue
        self._dcc_queue.put_nowait((filename, ip, port, filesize))

    async def search(self, term: str) -> Path:
        # Send our message to the server
        self.bot.privmsg("#ebooks", term)
        log.info("Message sent: %s", term)

        # Wait for a response back
        # If we got one, its probably an offer for a file
        # Lets yank out the data
        try:
            filename, ip, port, filesize = await asyncio.wait_for(
                self._dcc_queue.get(), 60
            )
        except asyncio.TimeoutError:
            raise TimeoutError(f"No DCC offer received within {60}s for '{term}'")

        dest = DOWNLOADS_DIR / filename
        log.info("DCC SEND offer: %s from %s:%s (%s bytes)", filename, ip, port, filesize)
        # If we've made it this far, we probably have a file
        # Lets receive it, and extract if its a zip
        await receive_dcc(ip, port, filesize, dest)
        log.info("Download complete: %s", dest)
        if dest.suffix.lower() == ".zip":
            dest = extract_ebook(dest, term)
            log.info("Extracted: %s", dest)
        return dest
