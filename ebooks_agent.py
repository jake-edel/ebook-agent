"""
IRC ebooks agent — connects to IRCHighway #ebooks, submits @search,
downloads the resulting DCC SEND .zip file, returns its local path.
"""

import asyncio
import logging
import os
import re
import socket
import struct
import zipfile
from pathlib import Path

import irc3

DOWNLOADS_DIR = Path(__file__).parent / "downloads"
DOWNLOADS_DIR.mkdir(exist_ok=True)

DCC_SEND_RE = re.compile(
    r"\x01DCC SEND (\S+) (\d+) (\d+) (\d+)\x01"
)

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
        await _receive_dcc(ip, port, filesize, dest)
        log.info("Download complete: %s", dest)
        if dest.suffix.lower() == ".zip":
            dest = _extract_ebook(dest, term)
            log.info("Extracted: %s", dest)
        return dest


async def _receive_dcc(ip: str, port: int, filesize: int, dest: Path):
    reader, writer = await asyncio.open_connection(ip, port)
    received = 0
    with open(dest, "wb") as f:
        while received < filesize:
            chunk = await reader.read(4096)
            if not chunk:
                break
            f.write(chunk)
            received += len(chunk)
            writer.write(struct.pack("!I", received))
            await writer.drain()
    writer.close()
    await writer.wait_closed()


_EBOOK_EXTS = {".txt", ".epub", ".mobi", ".pdf", ".doc", ".docx", ".rtf", ".azw", ".azw3"}


def _extract_ebook(zip_path: Path, term: str) -> Path:
    safe_term = re.sub(r'[<>:"/\\|?*]', "_", term)
    with zipfile.ZipFile(zip_path) as zf:
        members = [m for m in zf.infolist() if not m.is_dir()]
        members.sort(key=lambda m: (Path(m.filename).suffix.lower() not in _EBOOK_EXTS, m.filename))
        if not members:
            raise ValueError(f"Zip {zip_path.name} contains no files")
        target = members[0]
        suffix = Path(target.filename).suffix.lower() or ".txt"
        out_path = DOWNLOADS_DIR / f"{safe_term}{suffix}"
        out_path.write_bytes(zf.read(target.filename))
    zip_path.unlink()
    return out_path


class EbooksAgent:
    def __init__(self, nick: str = "fierro_viejo"):
        self._nick = nick
        self._bot: irc3.IrcBot | None = None
        self._plugin: EbooksPlugin | None = None

    def _build_bot(self) -> irc3.IrcBot:
        bot = irc3.IrcBot.from_config({
            "nick": self._nick,
            "autojoins": [],
            "host": "irc.irchighway.net",
            "port": 6667,
            "ssl": False,
            "includes": [
                "irc3.plugins.core",
                "irc3.plugins.userlist",
            ],
        })
        bot.include(EbooksPlugin)
        plugin = bot.get_plugin(EbooksPlugin)
        bot.attach_events(
            irc3.event(irc3.rfc.CONNECTED, plugin.on_connected),
            irc3.event(
                r"(?P<mask>\S+) (?:PRIVMSG|NOTICE) (?P<target>\S+) :(?P<text>.*\x01DCC SEND.*)",
                plugin.on_dcc_send,
            ),
        )
        return bot

    async def start(self, timeout: int = 30):
        self._bot = self._build_bot()
        self._plugin = self._bot.get_plugin(EbooksPlugin)
        self._bot.create_connection()
        await asyncio.wait_for(self._plugin._connected.wait(), timeout=timeout)

    def stop(self):
        if self._bot:
            self._bot.quit("bye")

    async def search(self, term: str, timeout: int = 60) -> Path:
        if self._plugin is None:
            raise RuntimeError("Not connected — call start() first")
        return await self._plugin.search(term, timeout=timeout)


async def _main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    agent = EbooksAgent()
    await agent.start()

    loop = asyncio.get_event_loop()
    try:
        while True:
            try:
                term = await loop.run_in_executor(None, lambda: input("search> "))
            except EOFError:
                break
            term = term.strip()
            if not term:
                continue
            try:
                path = await agent.search(term)
                print(path)
            except TimeoutError as e:
                print(f"Error: {e}")
    except (asyncio.CancelledError, KeyboardInterrupt):
        pass
    finally:
        agent.stop()


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        pass
