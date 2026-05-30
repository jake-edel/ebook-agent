"""DCC file transfer and ebook extraction."""

import asyncio
import re
import struct
import zipfile
from pathlib import Path

DOWNLOADS_DIR = Path(__file__).parent / "downloads"
DOWNLOADS_DIR.mkdir(exist_ok=True)

DCC_SEND_RE = re.compile(
    r"\x01DCC SEND (\S+) (\d+) (\d+) (\d+)\x01"
)

_EBOOK_EXTS = {".txt", ".epub", ".mobi", ".pdf", ".doc", ".docx", ".rtf", ".azw", ".azw3"}


async def receive_dcc(ip: str, port: int, filesize: int, dest: Path):
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


def extract_ebook(zip_path: Path, term: str) -> Path:
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
