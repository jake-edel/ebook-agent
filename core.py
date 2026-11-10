"""The core interface: what any frontend can ask of the agent and hear back.

This is the contract between the IRC/DCC machinery and whatever drives it —
a TUI in the same process, a socket server, a script, or (later, maybe) a
client talking to a daemon. It deliberately knows nothing about any of them.

The shape: commands in, events out, state on request.

- **Commands** return immediately. `search()` and `request()` hand back a
  `Request` snapshot with an id; the work then happens in the background,
  because on IRC a reply can take seconds or many minutes (bot queues).
- **Events** report everything that happens afterwards. Each subscriber gets
  its own stream, so several frontends can listen at once.
- **State** (`status()`, `requests()`) gives a full picture at any time, so a
  frontend that starts late, or reconnects, can catch up without replaying events.

Everything that crosses this boundary is a frozen dataclass of plain values
(str, int, float, bool, None, enums, paths, tuples). No asyncio objects, no
callbacks. `to_dict()` turns any of them into JSON-ready data, which is what
keeps a later daemon/client split a transport problem, not a redesign.
"""

from __future__ import annotations

import enum
from collections.abc import AsyncIterator
from dataclasses import dataclass, fields, is_dataclass
from pathlib import Path
from typing import Any, Protocol


# --- requests ---------------------------------------------------------------

class RequestKind(enum.StrEnum):
    # `@search <query>`: the reply is a zipped .txt of results, saved to searches/
    SEARCH = "search"
    # `!Bot <file>`: the reply is the book itself, saved to downloads/
    BOOK = "book"


class RequestState(enum.StrEnum):
    # Accepted, but not sent yet (e.g. waiting for the IRC connection)
    PENDING = "pending"
    # Sent to the channel; waiting for the bot to make a DCC offer
    WAITING = "waiting"
    # The bot told us we're in its queue (see Request.queue_position)
    QUEUED = "queued"
    # Offer accepted; bytes are arriving (see Request.received / size)
    TRANSFERRING = "transferring"
    # Terminal states
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def finished(self) -> bool:
        return self in (RequestState.DONE, RequestState.FAILED, RequestState.CANCELLED)


@dataclass(frozen=True)
class Request:
    """A snapshot of one search or book request. A new snapshot replaces the old one on every change."""

    id: str
    kind: RequestKind
    # Exactly what was (or will be) sent to the channel, e.g. "@search dune"
    line: str
    # The nick we expect the offer from: the "!Bot" for a book, None if any
    # channel member may answer (searches)
    bot: str | None
    state: RequestState
    # Unix timestamps
    created_at: float
    updated_at: float
    # Set once the bot reports a queue position
    queue_position: int | None = None
    # Set once an offer is accepted
    filename: str | None = None
    size: int | None = None
    received: int = 0
    # Set when DONE: where the results file or book was saved
    path: Path | None = None
    # Set when FAILED: a human-readable reason
    error: str | None = None


# --- connection -------------------------------------------------------------

@dataclass(frozen=True)
class AgentStatus:
    connected: bool
    server: str
    nick: str
    channel: str
    # Address we actually reached and its certificate fingerprint (TLS is
    # encrypt-only; these make a change of server visible)
    peer: str | None = None
    tls_fingerprint: str | None = None


# --- events -----------------------------------------------------------------

@dataclass(frozen=True)
class StatusChanged:
    """Connection state changed: connected, joined, disconnected, reconnecting."""
    status: AgentStatus
    reason: str | None = None


@dataclass(frozen=True)
class RequestUpdated:
    """A request changed state or made progress. Carries the full new snapshot.

    Progress updates during a transfer are rate-limited, so a fast download
    doesn't flood subscribers.
    """
    request: Request


@dataclass(frozen=True)
class BotMessage:
    """A NOTICE or PRIVMSG to us from a bot we have a request with.

    e.g. "You are in queue position 2", "File not found". `request_id` links it
    to the request it most likely belongs to, when we can tell.
    """
    nick: str
    text: str
    request_id: str | None = None


Event = StatusChanged | RequestUpdated | BotMessage


# --- the interface ----------------------------------------------------------

class AgentService(Protocol):
    """What frontends program against. EbooksAgent will implement this."""

    async def start(self) -> None:
        """Connect and join the channel. Raises TimeoutError if that fails."""
        ...

    async def stop(self) -> None:
        """Cancel outstanding requests, send QUIT and disconnect."""
        ...

    def status(self) -> AgentStatus: ...

    def requests(self) -> tuple[Request, ...]:
        """Every request this session, oldest first, finished ones included."""
        ...

    def search(self, query: str) -> Request:
        """Start an `@search <query>`. Returns at once; follow it via events or wait()."""
        ...

    def request(self, line: str) -> Request:
        """Start a book request: a `!Bot ...` line, typically ResultItem.request.

        Raises ValueError if `line` doesn't start with `!<bot>`.
        """
        ...

    def cancel(self, request_id: str) -> None:
        """Stop waiting for or receiving a request. No-op if it already finished."""
        ...

    async def wait(self, request_id: str) -> Request:
        """Wait until a request finishes and return its final snapshot.

        A convenience for simple callers (scripts, a one-shot socket command).
        Raises KeyError for an unknown id.
        """
        ...

    def events(self) -> AsyncIterator[Event]:
        """A new, independent stream of events from now on.

        Each call gives its own stream. Stop iterating (or close it) to unsubscribe.
        """
        ...


# --- serialization ----------------------------------------------------------

def to_dict(obj: Any) -> Any:
    """Convert a core value (snapshot, status, event) into JSON-ready data.

    Events get a "type" field with the class name, so a reader can tell them
    apart, e.g. {"type": "RequestUpdated", "request": {...}}.
    """
    if is_dataclass(obj) and not isinstance(obj, type):
        data = {f.name: to_dict(getattr(obj, f.name)) for f in fields(obj)}
        if isinstance(obj, (StatusChanged, RequestUpdated, BotMessage)):
            data = {"type": type(obj).__name__, **data}
        return data
    if isinstance(obj, enum.Enum):
        return obj.value
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, (list, tuple)):
        return [to_dict(x) for x in obj]
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    raise TypeError(f"Not a core value: {obj!r}")
