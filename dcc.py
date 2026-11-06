"""DCC file transfer and ebook extraction."""

import asyncio
import ipaddress
import os
import re
import shutil
import struct
import zipfile
from dataclasses import dataclass
from pathlib import Path

DOWNLOADS_DIR = (Path(__file__).parent / "downloads").resolve()
DOWNLOADS_DIR.mkdir(exist_ok=True)

# Characters that are unsafe or troublesome in filenames: path separators,
# Windows-reserved punctuation, and ASCII control chars (incl. NUL)
_UNSAFE_CHARS_RE = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')
# Most filesystems cap a filename at 255 bytes; leave headroom
_MAX_NAME_BYTES = 200

# Largest file we'll accept, either over DCC or extracted from a zip.
# Ebooks are rarely more than a few MB; big illustrated PDFs can reach tens.
# Also keeps DCC's 32-bit byte-count acks far from overflowing (4 GiB).
MAX_DOWNLOAD_BYTES = 100 * 1024 * 1024  # 100 MiB
# Leave this much free disk space after a download
_DISK_HEADROOM_BYTES = 512 * 1024 * 1024  # 512 MiB
_CHUNK_SIZE = 64 * 1024


class DownloadError(Exception):
    """Base class for any refusal or failure while fetching a file."""


class UnsafeFilenameError(DownloadError, ValueError):
    """Raised when a filename can't be turned into a path inside DOWNLOADS_DIR."""


class FileTooLargeError(DownloadError, ValueError):
    """Raised when a download or extracted file would exceed MAX_DOWNLOAD_BYTES or free disk."""


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
# Anchored to the start of the message so ordinary chat that merely
# *mentions* "DCC SEND" somewhere isn't treated as an offer.
DCC_SEND_RE = re.compile(
    r'^\x01?DCC SEND (?:"(?P<quoted>[^"]+)"|(?P<bare>\S+)) (?P<ip>\d+) (?P<port>\d+) (?P<filesize>\d+)'
)

# Ports below 1024 are privileged service ports (ssh, http, smtp, ...).
# Real DCC senders use high ports; refusing low ones stops an offer from
# pointing us at someone's service. Port 0 means "reverse/passive DCC",
# which we don't support.
_MIN_DCC_PORT = 1024


class InvalidOfferError(DownloadError, ValueError):
    """Raised when a DCC SEND offer is malformed or points somewhere we won't connect."""


@dataclass(frozen=True)
class DccOffer:
    filename: str
    ip: str
    port: int
    filesize: int


def parse_dcc_offer(text: str) -> DccOffer | None:
    """Parse and validate a DCC SEND offer.

    Returns None if the text isn't a DCC SEND at all, raises InvalidOfferError
    if it is one but fails validation, otherwise returns the offer.
    """
    m = DCC_SEND_RE.match(text)
    if not m:
        return None

    # Unpack the named groups — quoted handles filenames with spaces,
    # bare handles single-word filenames without quotes
    filename = m.group("quoted") or m.group("bare")
    port = int(m.group("port"))
    filesize = int(m.group("filesize"))

    # DCC encodes the IPv4 address as a single 32-bit integer
    try:
        addr = ipaddress.IPv4Address(int(m.group("ip")))
    except ipaddress.AddressValueError:
        raise InvalidOfferError(f"Invalid IP in DCC offer: {m.group('ip')}")

    # Only connect to publicly routable addresses. This rejects loopback
    # (127/8), private LAN ranges, link-local, CGNAT, multicast, reserved and
    # 0.0.0.0 — so an offer can't make us poke at our own machine or network.
    # Note: Python counts multicast (224/4) as "global", so check it explicitly.
    if not addr.is_global or addr.is_multicast:
        raise InvalidOfferError(f"Refusing non-public DCC address {addr}")
    if not _MIN_DCC_PORT <= port <= 65535:
        raise InvalidOfferError(f"Refusing DCC port {port}")
    if filesize <= 0:
        raise InvalidOfferError(f"Refusing DCC offer with size {filesize}")
    # Checked here, before we ever connect, so an oversized offer costs nothing
    if filesize > MAX_DOWNLOAD_BYTES:
        raise FileTooLargeError(
            f"Refusing {filename!r}: {filesize:,} bytes exceeds the "
            f"{MAX_DOWNLOAD_BYTES:,}-byte limit"
        )

    return DccOffer(filename=filename, ip=str(addr), port=port, filesize=filesize)

_EBOOK_EXTS = {".txt", ".epub", ".mobi", ".pdf", ".doc", ".docx", ".rtf", ".azw", ".azw3"}


async def receive_dcc(ip: str, port: int, filesize: int, dest: Path):
    # Re-check here so callers can't bypass sanitization by passing a raw path
    if dest.resolve().parent != DOWNLOADS_DIR:
        raise UnsafeFilenameError(f"Refusing to write outside downloads dir: {dest}")
    # Callers should have gone through parse_dcc_offer, but enforce it here too
    if filesize > MAX_DOWNLOAD_BYTES:
        raise FileTooLargeError(f"{filesize:,} bytes exceeds the {MAX_DOWNLOAD_BYTES:,}-byte limit")
    _check_disk_space(filesize)

    # Open a direct TCP connection to the sender (bypasses IRC server entirely)
    reader, writer = await asyncio.open_connection(ip, port)
    received = 0
    try:
        # Open destination file for writing in binary mode (never via a symlink)
        with _open_for_write(dest) as file:
            while received < filesize:
                # Read a chunk of bytes from the connection — never more than the
                # bytes still owed, so a sender that keeps streaming past the size
                # it advertised can't make us write a byte more than agreed
                chunk = await reader.read(min(_CHUNK_SIZE, filesize - received))
                if not chunk:
                    break
                # Write chunk to file, and also ack back to the sender
                # how many total bytes we've received so far
                file.write(chunk)
                received += len(chunk)
                writer.write(struct.pack("!I", received))
                # Flush the write buffer — actually send the ack over the wire
                await writer.drain()
    except BaseException:
        # Failed or cancelled (e.g. timeout) — don't leave a partial file behind
        dest.unlink(missing_ok=True)
        raise
    finally:
        writer.close()
        try:
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass


def _check_disk_space(needed: int):
    free = shutil.disk_usage(DOWNLOADS_DIR).free
    if free - needed < _DISK_HEADROOM_BYTES:
        raise FileTooLargeError(
            f"Not enough disk space: need {needed:,} bytes, {free:,} free"
        )


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

        # Zip bomb guard, part 1: the zip's own header says how big the file
        # will be once decompressed. Refuse before decompressing anything.
        if target.file_size > MAX_DOWNLOAD_BYTES:
            raise FileTooLargeError(
                f"Refusing to extract {target.filename!r}: {target.file_size:,} bytes "
                f"uncompressed exceeds the {MAX_DOWNLOAD_BYTES:,}-byte limit"
            )
        _check_disk_space(target.file_size)

        # Write the file to our downloads directory using the sanitized search term as the filename.
        # Only the search term and extension are used — never the member's own path inside the zip
        out_path = safe_download_path(f"{term}{suffix}")
        try:
            # Zip bomb guard, part 2: the header can lie, so stream in chunks
            # and count what actually comes out rather than read() it all into memory
            written = 0
            with file.open(target) as src, _open_for_write(out_path) as out:
                while chunk := src.read(_CHUNK_SIZE):
                    written += len(chunk)
                    if written > MAX_DOWNLOAD_BYTES:
                        raise FileTooLargeError(
                            f"Refusing to extract {target.filename!r}: decompressed past "
                            f"the {MAX_DOWNLOAD_BYTES:,}-byte limit"
                        )
                    out.write(chunk)
        except BaseException:
            out_path.unlink(missing_ok=True)
            raise

    # Let go of the zip now that we've extracted what we need
    zip_path.unlink()

    # Let the calling function know where the file is
    return out_path
