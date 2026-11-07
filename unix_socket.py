"""
Unix socket server — the agent's input: accepts search terms, returns file paths.

The socket lives in a directory only the current user can access
($XDG_RUNTIME_DIR, normally /run/user/<uid>), is created mode 0600, and
rejects connections from any other user.

Test: echo "dune frank herbert" | socat - UNIX-CONNECT:$XDG_RUNTIME_DIR/ebooks.sock
"""

import asyncio
import logging
import os
import socket
import stat
import struct
import tempfile
from pathlib import Path

from dcc import DownloadError

log = logging.getLogger(__name__)

SOCKET_NAME = "ebooks.sock"


class UnsafeSocketPathError(RuntimeError):
    """The socket's directory or path isn't private to us."""


class SocketUnavailableError(RuntimeError):
    """The socket server couldn't be started."""


def _is_private_dir(path: Path) -> bool:
    """True if `path` is a real directory (not a symlink) owned by us with no group/other access."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    return (
        stat.S_ISDIR(st.st_mode)
        and st.st_uid == os.getuid()
        and st.st_mode & 0o077 == 0
    )


def socket_dir() -> Path:
    """Directory to put the socket in, guaranteed private to the current user."""
    # Preferred: the per-user runtime dir systemd/logind creates (0700, ours)
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if runtime and _is_private_dir(Path(runtime)):
        return Path(runtime)
    if runtime:
        log.warning("XDG_RUNTIME_DIR %s is not private; using fallback", runtime)

    # Fallback: our own 0700 directory under the system temp dir. mkdir with
    # mode 0700 is atomic; if something already exists there (possibly planted
    # by another user) we only accept it if it passes the same checks.
    fallback = Path(tempfile.gettempdir()) / f"ebooks-{os.getuid()}"
    try:
        fallback.mkdir(mode=0o700)
    except FileExistsError:
        pass
    if not _is_private_dir(fallback):
        raise UnsafeSocketPathError(
            f"{fallback} exists but is not a private directory owned by you — "
            "refusing to put the socket there"
        )
    return fallback


def socket_path() -> Path:
    return socket_dir() / SOCKET_NAME


def _remove_stale_socket(path: Path):
    """Remove a leftover socket from a previous run — but only if it IS a socket."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(st.st_mode):
        raise UnsafeSocketPathError(f"{path} exists and is not a socket — refusing to remove it")
    path.unlink()


def _bind_private_socket(path: Path) -> socket.socket:
    """Create and bind a listening Unix socket that is mode 0600 from the moment it exists."""
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    # bind() creates the socket file using the process umask. Tighten the
    # umask just for this call so the file is never briefly group/world
    # accessible. bind() is synchronous, so nothing else runs in between.
    old_umask = os.umask(0o177)
    try:
        sock.bind(str(path))
    except BaseException:
        sock.close()
        raise
    finally:
        os.umask(old_umask)
    os.chmod(path, 0o600)  # belt and braces
    return sock


def _peer_uid(writer: asyncio.StreamWriter) -> int | None:
    """UID of the process on the other end of the socket (Linux SO_PEERCRED)."""
    sock = writer.get_extra_info("socket")
    if sock is None or not hasattr(socket, "SO_PEERCRED"):
        return None
    creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
    _pid, uid, _gid = struct.unpack("3i", creds)
    return uid


# Factory function that closes over agent, making it available to handle()
# without changing its signature — asyncio expects (reader, writer) only
def _make_handler(agent):
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            # Directory and file permissions should already keep other users out;
            # verify the connecting process's UID anyway in case they're loosened
            uid = _peer_uid(writer)
            if uid is not None and uid != os.getuid():
                log.warning("Rejected socket connection from uid %s", uid)
                return

            # Read line by line, decode bytes to text
            data = await reader.readline()
            term = data.decode().strip()

            # Nothing there — close up shop
            if not term:
                return

            log.info("Request: %s", term)
            try:
                # Pass the search term to our agent
                path = await agent.search(term)
                reply = f"{path}\n"
                log.info("Saved: %s", path)
            except (TimeoutError, DownloadError) as e:
                reply = f"error: {e}\n"
                log.warning("Failed: %s — %s", term, e)
            except Exception:
                # A bug, not a normal failure — but the client still gets an
                # answer instead of hanging, and the details go to the log
                log.exception("Search for %r failed unexpectedly", term)
                reply = "error: internal error (see agent log)\n"

            # Write the response back to the external source
            writer.write(reply.encode())
            try:
                # Flush once we're done — caller has probably already closed their end by now
                await writer.drain()
            except ConnectionError:
                # client (e.g. socat) closed its end before drain finished
                pass
        except Exception:
            # e.g. an oversized line or undecodable bytes from the client
            log.exception("Socket connection failed")
        finally:
            # Always release the connection, whatever happened above
            writer.close()

    return handle


async def unix_socket(agent, path: Path | None = None):
    try:
        path = path or socket_path()
        _remove_stale_socket(path)
        sock = _bind_private_socket(path)
    except (UnsafeSocketPathError, OSError) as e:
        # The socket is the agent's only input, so this is fatal
        raise SocketUnavailableError(f"Can't open Unix socket: {e}") from e

    # Pass asyncio our handler, which tells the socket server what to do when bytes arrive
    # asyncio because who knows when those bytes are showing up
    server = await asyncio.start_unix_server(_make_handler(agent), sock=sock)
    log.info("Listening on %s", path)
    log.info("Send a search with: echo 'Dune Frank Herbert' | socat - UNIX-CONNECT:%s", path)
    log.info("Ready. Press Ctrl+C to stop.")
    try:
        async with server:
            await server.serve_forever()
    finally:
        # Don't leave the socket file behind when we shut down
        path.unlink(missing_ok=True)
