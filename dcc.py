"""DCC file transfer and ebook extraction."""

import asyncio
import errno
import ipaddress
import os
import re
import secrets
import shutil
import struct
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path

from config import DownloadsConfig

# Characters that are unsafe or troublesome in filenames: path separators,
# Windows-reserved punctuation, and ASCII control chars (incl. NUL)
_UNSAFE_CHARS_RE = re.compile(r'[\x00-\x1f\x7f<>:"/\\|?*]')
# Most filesystems cap a filename at 255 bytes; leave headroom
_MAX_NAME_BYTES = 200
_CHUNK_SIZE = 64 * 1024


class DownloadError(Exception):
    """Base class for any refusal or failure while fetching a file."""


class UnsafeFilenameError(DownloadError, ValueError):
    """Raised when a filename can't be turned into a path inside one of the save directories."""


class FileTooLargeError(DownloadError, ValueError):
    """Raised when a download or extracted file would exceed the size limit or free disk."""


class TransferError(DownloadError):
    """Raised when the DCC connection can't be made, stalls, or breaks mid-transfer."""


class IncompleteDownloadError(TransferError):
    """Raised when the sender hangs up before sending every byte it advertised."""


class BadArchiveError(DownloadError, ValueError):
    """Raised when a downloaded zip is corrupt, unsupported, or empty."""


def _open_for_write(path: Path):
    """Create a new file for binary writing. Never truncates or follows an existing path."""
    # O_EXCL: fail if anything already exists at `path` (including a symlink,
    # dangling or not), so we can never clobber an existing file.
    # O_NOFOLLOW: belt and braces against symlinks on systems where O_EXCL
    # alone doesn't cover them.
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
    fd = os.open(path, flags, 0o644)
    return os.fdopen(fd, "wb")


def _open_part(dest: Path):
    """Create a fresh temporary file next to `dest` to download into.

    Returns (part_path, file). The random tag means a leftover .part from a
    crashed run, or a concurrent download of the same name, never collides.
    """
    part = dest.with_name(f"{dest.name}.{secrets.token_hex(4)}.part")
    return part, _open_for_write(part)


# Filesystems that can't hard-link (FAT, some FUSE/network mounts)
_NO_LINK_ERRNOS = {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.EMLINK}


def _finalize(part: Path, dest: Path) -> Path:
    """Move a completed .part file to `dest`, or "name (1).ext" etc. if taken.

    Never overwrites anything: an existing book with the same name is kept,
    and the new file gets the first free numbered name. Returns the final path.
    """
    for n in range(1000):
        candidate = dest if n == 0 else dest.with_name(f"{dest.stem} ({n}){dest.suffix}")
        try:
            # link() is atomic and refuses to replace an existing path — unlike
            # rename(), which silently overwrites
            os.link(part, candidate, follow_symlinks=False)
        except FileExistsError:
            continue
        except OSError as e:
            if e.errno not in _NO_LINK_ERRNOS:
                raise
            # No hard links here: fall back to check-then-rename. There's a
            # tiny race window, but only against our own processes.
            if os.path.lexists(candidate):
                continue
            os.rename(part, candidate)
            return candidate
        part.unlink()
        return candidate
    raise DownloadError(f"No free filename for {dest.name} in {dest.parent}")

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


def parse_dcc_offer(text: str, max_bytes: int) -> DccOffer | None:
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
    if filesize > max_bytes:
        raise FileTooLargeError(
            f"Refusing {filename!r}: {filesize:,} bytes exceeds the "
            f"{max_bytes:,}-byte limit"
        )

    return DccOffer(filename=filename, ip=str(addr), port=port, filesize=filesize)

_EBOOK_EXTS = {".txt", ".epub", ".mobi", ".pdf", ".doc", ".docx", ".rtf", ".azw", ".azw3"}


# What zipfile raises for archives it can't read: corrupt or truncated data
# (BadZipFile, zlib.error, EOFError), unsupported compression methods
# (NotImplementedError), and password-protected entries (RuntimeError)
_ZIP_ERRORS = (zipfile.BadZipFile, zlib.error, EOFError, NotImplementedError, RuntimeError)


class Downloads:
    """Saves files from DCC offers, within the limits of one DownloadsConfig.

    Books go to `dir`; search results files go to `searches_dir`. Those are
    the only two directories anything is ever written into.
    """

    def __init__(self, config: DownloadsConfig):
        self.config = config
        # Resolved up front: safe_path compares resolved paths against these
        self.dir = config.dir.resolve()
        self.searches_dir = config.searches_dir.resolve()
        self.save_dirs = (self.dir, self.searches_dir)
        # Largest file we'll accept, either over DCC or extracted from a zip.
        # Ebooks are rarely more than a few MB; big illustrated PDFs can reach
        # tens. Also keeps DCC's 32-bit byte-count acks far from overflowing.
        self.max_bytes = config.max_size_bytes

    def create_dirs(self):
        """Create the save directories. Call once at startup."""
        for d in self.save_dirs:
            d.mkdir(parents=True, exist_ok=True)

    def parse_offer(self, text: str) -> DccOffer | None:
        """parse_dcc_offer, with this config's size limit."""
        return parse_dcc_offer(text, self.max_bytes)

    def safe_path(self, name: str, directory: Path | None = None) -> Path:
        """Map an untrusted filename to a path guaranteed to live directly in `directory`.

        `directory` defaults to the downloads dir and must be one of the save
        directories. The name comes from a remote IRC bot, so treat it as hostile:
        drop any directory components (either separator style), neutralize odd
        characters, and refuse anything that still resolves outside `directory`.
        """
        directory = self.dir if directory is None else directory.resolve()
        if directory not in self.save_dirs:
            raise UnsafeFilenameError(f"Not a save directory: {directory}")
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
        # present) and confirm it sits directly inside `directory`
        dest = (directory / base).resolve()
        if dest.parent != directory:
            raise UnsafeFilenameError(f"Refusing path outside {directory}: {name!r}")
        return dest

    async def receive(self, ip: str, port: int, filesize: int, dest: Path) -> Path:
        """Download a DCC file into `dest`'s directory and return where it was saved.

        `dest` must sit directly in one of the save dirs (use safe_path to
        build it).

        The returned path is `dest`, or a numbered variant like "Dune (1).epub" if
        a file by that name already exists. Raises a DownloadError subclass on any
        failure, in which case nothing is left on disk.
        """
        # Re-check here so callers can't bypass sanitization by passing a raw path
        directory = dest.resolve().parent
        if directory not in self.save_dirs:
            raise UnsafeFilenameError(f"Refusing to write outside the save dirs: {dest}")
        # Callers should have gone through parse_dcc_offer, but enforce it here too
        if filesize > self.max_bytes:
            raise FileTooLargeError(f"{filesize:,} bytes exceeds the {self.max_bytes:,}-byte limit")
        self._check_disk_space(filesize, directory)

        # Open a direct TCP connection to the sender (bypasses IRC server entirely).
        # Done before creating any file, so a dead sender leaves nothing behind.
        try:
            async with asyncio.timeout(self.config.connect_timeout):
                reader, writer = await asyncio.open_connection(ip, port)
        except TimeoutError:
            raise TransferError(
                f"Could not connect to {ip}:{port} within {self.config.connect_timeout}s"
            ) from None
        except OSError as e:
            raise TransferError(f"Could not connect to {ip}:{port}: {e}") from e

        part = None
        try:
            # Download into a temporary .part file (never via a symlink); it only
            # gets the real name once every byte has arrived
            part, file = _open_part(dest)
            received = 0
            with file:
                while received < filesize:
                    # The stall timeout restarts for every chunk: it limits idle
                    # time, not the length of the whole transfer
                    async with asyncio.timeout(self.config.stall_timeout):
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

            if received != filesize:
                raise IncompleteDownloadError(
                    f"Sender closed the connection after {received:,} of {filesize:,} bytes"
                )
            final = _finalize(part, dest)
            part = None  # it's been moved into place; nothing to clean up
            return final
        except TimeoutError:
            # TimeoutError is a subclass of OSError, so this must come first
            raise TransferError(
                f"Transfer stalled: no data from {ip}:{port} for {self.config.stall_timeout}s"
            ) from None
        except OSError as e:
            raise TransferError(f"Transfer from {ip}:{port} failed: {e}") from e
        finally:
            # Failed or cancelled — remove only our own .part file. Any existing
            # file at `dest` was never touched.
            if part is not None:
                part.unlink(missing_ok=True)
            writer.close()
            try:
                await writer.wait_closed()
            except (ConnectionError, OSError):
                pass

    def _check_disk_space(self, needed: int, directory: Path):
        # Normally created by create_dirs(); make sure it exists for direct callers too
        directory.mkdir(parents=True, exist_ok=True)
        free = shutil.disk_usage(directory).free
        if free - needed < self.config.disk_headroom_bytes:
            raise FileTooLargeError(
                f"Not enough disk space: need {needed:,} bytes, {free:,} free"
            )

    def extract(self, zip_path: Path, name: str | None = None) -> Path:
        """Extract the best ebook candidate from a zip, delete the zip, return the ebook's path.

        The file is extracted next to the zip. It's named `name` plus the member's
        extension, or, if `name` is None, after the member itself (its base name
        only, sanitized). Like receive(), never overwrites an existing file.
        Raises BadArchiveError if the zip can't be read.
        """
        # Most ebooks come uncompressed directly over DCC. This handles the minority
        # of bots that bundle files in a zip — flexible, but rarely needed in practice.
        try:
            out_path = self._extract(zip_path, name)
        except _ZIP_ERRORS as e:
            raise BadArchiveError(f"Could not extract {zip_path.name}: {e}") from e

        # Let go of the zip now that we've extracted what we need
        zip_path.unlink()

        # Let the calling function know where the file is
        return out_path

    def _extract(self, zip_path: Path, name: str | None) -> Path:
        with zipfile.ZipFile(zip_path) as file:
            # Get all members, skipping any directories
            members = [m for m in file.infolist() if not m.is_dir()]

            # Sort so known ebook extensions float to the top.
            # The condition is inverted (not in) because False sorts before True —
            # files that ARE ebook extensions get False and sort first
            members.sort(key=lambda m: (Path(m.filename).suffix.lower() not in _EBOOK_EXTS, m.filename))

            if not members:
                raise BadArchiveError(f"Zip {zip_path.name} contains no files")

            # Target only the first file found — the best ebook candidate after sorting
            target = members[0]
            suffix = Path(target.filename).suffix.lower() or ".txt"

            # Zip bomb guard, part 1: the zip's own header says how big the file
            # will be once decompressed. Refuse before decompressing anything.
            if target.file_size > self.max_bytes:
                raise FileTooLargeError(
                    f"Refusing to extract {target.filename!r}: {target.file_size:,} bytes "
                    f"uncompressed exceeds the {self.max_bytes:,}-byte limit"
                )
            self._check_disk_space(target.file_size, zip_path.parent)

            # Never use the member's path inside the zip — at most its base name,
            # which safe_path keeps inside the zip's own directory
            directory = zip_path.parent
            if name is None:
                try:
                    dest = self.safe_path(target.filename, directory)
                except UnsafeFilenameError:
                    # e.g. a member called "..." — fall back to the zip's own name
                    dest = self.safe_path(f"{zip_path.stem}{suffix}", directory)
            else:
                dest = self.safe_path(f"{name}{suffix}", directory)
            part = None
            try:
                # Zip bomb guard, part 2: the header can lie, so stream in chunks
                # and count what actually comes out rather than read() it all into memory
                written = 0
                part, out = _open_part(dest)
                with file.open(target) as src, out:
                    while chunk := src.read(_CHUNK_SIZE):
                        written += len(chunk)
                        if written > self.max_bytes:
                            raise FileTooLargeError(
                                f"Refusing to extract {target.filename!r}: decompressed past "
                                f"the {self.max_bytes:,}-byte limit"
                            )
                        out.write(chunk)
                final = _finalize(part, dest)
                part = None
                return final
            finally:
                if part is not None:
                    part.unlink(missing_ok=True)
