"""Unix socket server: lets other processes (scripts, Neovim's :Ebook) drive the agent.

The protocol is JSON lines. A client connects, sends one command, and reads
replies until the server closes the connection:

    -> {"cmd": "search", "query": "dune frank herbert"}
    -> {"cmd": "request", "line": "!Bsk Frank Herbert - Dune.epub"}
       <- {"type": "RequestUpdated", "request": {"id": "1", "state": "pending", ...}}
       <- {"type": "BotMessage", "nick": "Bsk", "text": "...", "request_id": "1"}
       <- ... every change, progress included, ending with the RequestUpdated
          whose state is "done" (see request.path), "failed" (request.error)
          or "cancelled". For a book with a post-download hook, the replies
          go on until the hook's done: request.hook is "running" on the
          "done" one, then "done" or "failed" (request.hook_error)

    -> {"cmd": "status"}
       <- {"type": "Status", "status": {"state": "ready", "server": ..., ...}}

Replies are the core's own events (core.to_dict), limited to the client's
request. A command that can't be carried out gets
{"type": "Error", "error": "..."} instead, as does a client still waiting
when the agent shuts down. A client that hangs up early only stops the
replies: the request carries on, and the file is still saved.

The socket lives in a directory only the current user can access
($XDG_RUNTIME_DIR, normally /run/user/<uid>), is created mode 0600, and
rejects connections from any other user. `ebooks_agent.py --socket-path`
prints where it is.

Test: echo '{"cmd": "search", "query": "dune"}' | socat -t 600 - UNIX-CONNECT:$(python ebooks_agent.py --socket-path)
"""

import asyncio
import json
import logging
import os
import socket
import stat
import struct
import tempfile
from pathlib import Path

from core import BotMessage, RequestUpdated, to_dict

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


class SocketInUseError(SocketUnavailableError):
    """Another agent is already serving the socket."""


def _is_live(path: Path) -> bool:
    """True if something accepts connections on the socket at `path`."""
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(1)
    try:
        probe.connect(str(path))
    except OSError:  # refused (nobody listening), gone, or timed out
        return False
    else:
        return True
    finally:
        probe.close()


def _remove_stale_socket(path: Path):
    """Remove a leftover socket from a previous run — but only if it IS a
    socket, and nothing is listening on it any more."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(st.st_mode):
        raise UnsafeSocketPathError(f"{path} exists and is not a socket — refusing to remove it")
    # Otherwise a second agent would silently take the socket over, leaving
    # the first running with nothing able to reach it
    if _is_live(path):
        raise SocketInUseError(f"Another agent is already running (listening on {path})")
    path.unlink()


def claim_socket_path(path: Path | None = None) -> Path:
    """Where to serve the socket, checked to be free (a stale socket is removed).

    Raises SocketInUseError if another agent is serving it, or
    SocketUnavailableError if the location isn't safe.
    """
    try:
        path = path or socket_path()
        _remove_stale_socket(path)
    except (UnsafeSocketPathError, OSError) as e:
        raise SocketUnavailableError(f"Can't open Unix socket: {e}") from e
    return path


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


# A client gets this long to send its command, so an idle one can't hold a
# connection open forever
COMMAND_TIMEOUT = 10.0
# The longest command line we'll read. Requests themselves are capped much
# lower (plugin.MAX_LINE_BYTES); this leaves room for JSON escaping.
MAX_COMMAND_BYTES = 8192

# Each command and the one string argument it takes
_COMMANDS = {"search": "query", "request": "line", "status": None}


class _ClientError(Exception):
    """A bad command; the message is sent back to the client."""


def _parse_command(data: bytes) -> tuple[str, str | None]:
    """Turn a command line into (command, argument). Raises _ClientError."""
    usage = ('expected {"cmd": "search", "query": ...}, {"cmd": "request", "line": ...} '
             'or {"cmd": "status"}')
    try:
        cmd = json.loads(data)
    except ValueError:  # bad JSON or bad UTF-8
        raise _ClientError(f"not a JSON command: {usage}") from None
    name = cmd.get("cmd") if isinstance(cmd, dict) else None
    if name not in _COMMANDS:
        raise _ClientError(f"unknown command: {usage}")
    arg = _COMMANDS[name]
    expected = {"cmd", arg} if arg else {"cmd"}
    if cmd.keys() != expected:
        raise _ClientError(f"{name} takes {', '.join(sorted(expected))}, got {', '.join(sorted(cmd))}")
    if arg and not isinstance(cmd[arg], str):
        raise _ClientError(f"{name}: {arg} must be a string")
    return name, cmd.get(arg)


def _line(message: dict) -> bytes:
    return json.dumps(message).encode() + b"\n"


async def _send(writer: asyncio.StreamWriter, message: dict):
    writer.write(_line(message))
    await writer.drain()


def _error(text: str) -> dict:
    return {"type": "Error", "error": text}


async def _serve_client(agent, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
    """Read one command, carry it out, and stream back the replies."""
    try:
        async with asyncio.timeout(COMMAND_TIMEOUT):
            data = await reader.readline()
    except TimeoutError:
        raise _ClientError(f"no command within {COMMAND_TIMEOUT:g}s") from None
    except ValueError:  # the line is over the reader's limit
        raise _ClientError(f"command longer than {MAX_COMMAND_BYTES} bytes") from None
    if not data.strip():
        return  # connected and hung up

    name, arg = _parse_command(data)
    if name == "status":
        await _send(writer, {"type": "Status", "status": to_dict(agent.status())})
        return

    # Subscribe before starting the request, so not even its first event is missed
    events = agent.events()
    try:
        try:
            req = agent.search(arg) if name == "search" else agent.request(arg)
        except ValueError as e:  # e.g. an empty query, or a line that isn't "!Bot <file>"
            raise _ClientError(str(e)) from None
        log.info("Request: %s", req.line)
        async for event in events:
            if isinstance(event, RequestUpdated) and event.request.id == req.id:
                await _send(writer, to_dict(event))
                final = event.request
                if final.settled:
                    if final.path:
                        log.info("Saved: %s", final.path)
                    else:
                        log.warning("Failed: %s — %s", final.line, final.error or final.state.value)
                    return
            elif isinstance(event, BotMessage) and event.request_id == req.id:
                await _send(writer, to_dict(event))
        # The stream ended: the agent stopped without finishing the request
        raise _ClientError("agent stopped")
    finally:
        await events.aclose()


# Factory function that closes over agent, making it available to handle()
# without changing its signature — asyncio expects (reader, writer) only
def _make_handler(agent, handlers: set[asyncio.Task]):
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        # Tracked so shutdown can end connections still following a request
        handlers.add(task := asyncio.current_task())
        try:
            # Directory and file permissions should already keep other users out;
            # verify the connecting process's UID anyway in case they're loosened
            uid = _peer_uid(writer)
            if uid is not None and uid != os.getuid():
                log.warning("Rejected socket connection from uid %s", uid)
                return
            try:
                await _serve_client(agent, reader, writer)
            except _ClientError as e:
                log.warning("Socket command refused: %s", e)
                await _send(writer, _error(str(e)))
        except asyncio.CancelledError:
            # The agent is shutting down. Tell the client why, if it's still
            # there (no drain: we mustn't wait on a client now).
            writer.write(_line(_error("agent is shutting down")))
            raise
        except ConnectionError:
            # The client hung up while we were replying. Its request carries on.
            log.info("Socket client disconnected")
        except Exception:
            # A bug, not a normal failure — the client still gets an answer
            # instead of hanging, and the details go to the log
            log.exception("Socket connection failed")
            writer.write(_line(_error("internal error (see agent log)")))
        finally:
            handlers.discard(task)
            # Always release the connection, whatever happened above
            writer.close()

    return handle


async def unix_socket(agent, path: Path | None = None):
    """Serve the socket until cancelled. Raises SocketUnavailableError if it can't be opened."""
    # Checked again here: another agent may have started since an earlier claim
    path = claim_socket_path(path)
    try:
        sock = _bind_private_socket(path)
    except OSError as e:
        raise SocketUnavailableError(f"Can't open Unix socket: {e}") from e

    handlers: set[asyncio.Task] = set()
    server = await asyncio.start_unix_server(
        _make_handler(agent, handlers), sock=sock, limit=MAX_COMMAND_BYTES)
    log.info("Listening on %s", path)
    log.info("Ready. Press Ctrl+C to stop.")
    try:
        # Not serve_forever() or `async with server`: on the way out both wait
        # for every client to hang up, and a client following a request would
        # only do so when the request ends — which is after we've shut down.
        await asyncio.Event().wait()
    finally:
        server.close()
        for task in list(handlers):
            task.cancel()
        await asyncio.gather(*handlers, return_exceptions=True)
        await server.wait_closed()
        # Don't leave the socket file behind when we shut down
        path.unlink(missing_ok=True)
