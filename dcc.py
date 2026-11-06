"""DCC file transfer and ebook extraction."""

import asyncio
import os
import re
import struct
import zipfile
from pathlib import Path

DOWNLOADS_DIR = (Path(__file__).parent / "downloads").resolve()
DOWNLOADS_DIR.mkdir(exist_ok=True)

# Characters that are unsafe or troublesome in filenames: path separators,
# Windows-reserved punctuation, and ASCII control chars (incl. NUL)
_UNSAFE_CHARS_RE = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')
# Most filesystems cap a filename at 255 bytes; leave headroom
_MAX_NAME_BYTES = 200


class UnsafeFilenameError(ValueError):
    """Raised when a filename can't be turned into a path inside DOWNLOADS_DIR."""


def safe_download_path(name: str) -> Path:
    """Map an untrusted filename to a path guaranteed to live directly in DOWNLOADS_DIR.

    The name comes from a remote IRC bot, so treat it as hostile: drop any
    directory components (either separator style), neutralize odd characters,
    and refuse anything that still resolves outside the downloads directory.
    """
    # Keep only the final path component, whether the sender used / or \
    base = re.split(r"[/\\]", name)[-1]
    # Replace unsafe characters, trim whitespace, and strip leading dots so
    # we never produce ".", "..", or hidden dotfiles like ".bashrc"
    base = _UNSAFE_CHARS_RE.sub("_", base).strip().lstrip(".").strip()
    if not base:
        raise UnsafeFilenameError(f"Refusing unusable filename: {name!r}")

    # Truncate overly long names while preserving the extension
    if len(base.encode()) > _MAX_NAME_BYTES:
        stem, suffix = os.path.splitext(base)
        suffix = suffix[:16]
        budget = _MAX_NAME_BYTES - len(suffix.encode())
        stem = stem.encode()[:budget].decode(errors="ignore")
        base = stem + suffix

    # Belt and braces: resolve the final path (following any symlinks already
    # present) and confirm it sits directly inside DOWNLOADS_DIR
    dest = (DOWNLOADS_DIR / base).resolve()
    if dest.parent != DOWNLOADS_DIR:
        raise UnsafeFilenameError(f"Refusing path outside downloads dir: {name!r}")
    return dest


def _open_for_write(path: Path):
    """Open a file for binary writing without following a symlink at that path."""
    # O_NOFOLLOW makes open() fail if `path` is a symlink, closing the gap
    # between the resolve() check above and the actual write
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW
    fd = os.open(path, flags, 0o644)
    return os.fdopen(fd, "wb")

# Handles both quoted filenames (with spaces) and bare unquoted filenames.
# Some bots wrap filenames in quotes: DCC SEND "my file.epub" ip port size
# Others skip them:                   DCC SEND myfile.epub ip port size
# The \x01 CTCP delimiter is also optional — not all bots include it.
DCC_SEND_RE = re.compile(
    r'DCC SEND (?:"(?P<quoted>[^"]+)"|(?P<bare>\S+)) (?P<ip>\d+) (?P<port>\d+) (?P<filesize>\d+)'
)

_EBOOK_EXTS = {".txt", ".epub", ".mobi", ".pdf", ".doc", ".docx", ".rtf", ".azw", ".azw3"}


async def receive_dcc(ip: str, port: int, filesize: int, dest: Path):
    # Re-check here so callers can't bypass sanitization by passing a raw path
    if dest.resolve().parent != DOWNLOADS_DIR:
        raise UnsafeFilenameError(f"Refusing to write outside downloads dir: {dest}")

    # Open a direct TCP connection to the sender (bypasses IRC server entirely)
    reader, writer = await asyncio.open_connection(ip, port)
    received = 0
    # Open destination file for writing in binary mode (never via a symlink)
    with _open_for_write(dest) as file:
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

    with zipfile.ZipFile(zip_path) as file:
        # Get all members, skipping any directories
        members = [m for m in file.infolist() if not m.is_dir()]

        # Sort so known ebook extensions float to the top.
        # The condition is inverted (not in) because False sorts before True —
        # files that ARE ebook extensions get False and sort first
        members.sort(key=lambda m: (Path(m.filename).suffix.lower() not in _EBOOK_EXTS, m.filename))

        if not members:
            raise ValueError(f"Zip {zip_path.name} contains no files")

        # Target only the first file found — the best ebook candidate after sorting
        target = members[0]
        suffix = Path(target.filename).suffix.lower() or ".txt"

        # Write the file to our downloads directory using the sanitized search term as the filename.
        # Only the search term and extension are used — never the member's own path inside the zip
        out_path = safe_download_path(f"{term}{suffix}")
        with _open_for_write(out_path) as out:
            out.write(file.read(target.filename))

    # Let go of the zip now that we've extracted what we need
    zip_path.unlink()

    # Let the calling function know where the file is
    return out_path
