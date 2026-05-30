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
    # Open a direct TCP connection to the sender (bypasses IRC server entirely)
    reader, writer = await asyncio.open_connection(ip, port)
    received = 0
    # Open destination file for writing in binary mode
    with open(dest, "wb") as file:
        while received < filesize:
            # Read a chunk of bytes from the connection
            chunk = await reader.read(4096)
            if not chunk:
                break
            # Write chunk to file, and also ack back to the sender
            # how many total bytes we've received so far
            file.write(chunk)
            received += len(chunk)
            writer.write(struct.pack("!I", received))
            # Flush the write buffer — actually send the ack over the wire
            await writer.drain()
    writer.close()
    await writer.wait_closed()


def extract_ebook(zip_path: Path, term: str) -> Path:
    # Most ebooks come uncompressed directly over DCC. This handles the minority
    # of bots that bundle files in a zip — flexible, but rarely needed in practice.

    # Strip out any characters that are unsafe in a filename
    safe_term = re.sub(r'[<>:"/\\|?*]', "_", term)

    with zipfile.ZipFile(zip_path) as zipfile:
        # Get all members, skipping any directories
        members = [m for m in zipfile.infolist() if not m.is_dir()]

        # Sort so known ebook extensions float to the top.
        # The condition is inverted (not in) because False sorts before True —
        # files that ARE ebook extensions get False and sort first
        members.sort(key=lambda m: (Path(m.filename).suffix.lower() not in _EBOOK_EXTS, m.filename))

        if not members:
            raise ValueError(f"Zip {zip_path.name} contains no files")

        # Target only the first file found — the best ebook candidate after sorting
        target = members[0]
        suffix = Path(target.filename).suffix.lower() or ".txt"

        # Write the file to our downloads directory using the sanitized search term as the filename
        out_path = DOWNLOADS_DIR / f"{safe_term}{suffix}"
        out_path.write_bytes(zipfile.read(target.filename))

    # Let go of the zip now that we've extracted what we need
    zip_path.unlink()

    # Let the calling function know where the file is
    return out_path
