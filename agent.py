"""EbooksAgent — owns the IRC connection and implements the core interface (core.py)."""

import asyncio
import dataclasses
import hashlib
import itertools
import logging
import ssl
import time
from collections.abc import AsyncIterator

import irc3

from config import Config
from core import (
    AgentStatus,
    BotMessage,
    ConnectionState,
    Event,
    MembersChanged,
    Request,
    RequestKind,
    RequestState,
    RequestUpdated,
    StatusChanged,
)
from dcc import DownloadError
from hook import run_hook
from plugin import EbooksPlugin, check_query, check_request

log = logging.getLogger(__name__)

# How long stop() waits for the server to hang up after our QUIT
QUIT_TIMEOUT = 2.0

# Progress updates closer together than this are skipped, so a fast download
# doesn't flood subscribers
PROGRESS_INTERVAL = 0.25

# Channel joins and parts are gathered into one MembersChanged per this many
# seconds: the channel has hundreds of members coming and going
MEMBERS_INTERVAL = 2.0

# TLS is always on and isn't configurable. IRCHighway round-robins across
# several servers, each with its own self-signed certificate, and doesn't
# publish the list — so certificates can't be verified or pinned. We still use
# TLS: it keeps searches and nick private from passive observers (shared
# Wi-Fi, ISP). It does NOT stop an active man-in-the-middle; note DCC file
# transfers are plaintext regardless.


# Seconds between connection attempts after a drop or a failure, growing so
# a server that keeps refusing us isn't hammered (that's how IPs get banned).
# irc3 itself waits 2-3s before each retry; we add the rest. Back to the
# start once we've joined the channel again.
RECONNECT_DELAYS = (3, 10, 30, 60, 120, 300)
_IRC3_RETRY_WAIT = 3


class _IrcBot(irc3.IrcBot):
    # Set by EbooksAgent.stop(). irc3 retries a failed or dropped connection
    # with call_later() handles it doesn't keep, so they can't be cancelled:
    # they find this flag instead.
    stopped = False
    # Attempts since we were last in the channel (the first one included)
    attempts = 0
    # irc3 passes this to logging.config.dictConfig. Its own default gives
    # the "irc3" loggers a stderr handler that doesn't propagate, which
    # prints over the TUI and keeps them out of the log file, and, not being
    # incremental, closes every handler already set up. An empty incremental
    # config leaves logging alone: irc3's records go to the root logger like
    # ours.
    logging_config = {"version": 1, "incremental": True}

    def create_connection(self):
        if self.stopped:
            return self.loop
        if self.attempts:
            # A retry: irc3 has already waited a little; wait out the rest
            delay = RECONNECT_DELAYS[min(self.attempts, len(RECONNECT_DELAYS)) - 1]
            extra = delay - _IRC3_RETRY_WAIT
            self.attempts += 1
            if extra > 0:
                log.warning("Reconnecting to %s in %ds", self.config.host, delay)
                self.loop.call_later(extra, self._connect_now)
                return self.loop
            return self._connect_now()
        self.attempts += 1
        return self._connect_now()

    def _connect_now(self):
        if self.stopped:
            return self.loop
        return super().create_connection()

    def joined(self):
        """We're in the channel: the next drop starts the delays over."""
        self.attempts = 0

    def connection_made(self, f):
        if self.stopped:
            # An attempt that was still in progress when we stopped
            if not f.cancelled() and f.exception() is None:
                f.result()[0].close()
            return
        super().connection_made(f)

    def get_ssl_context(self):
        # irc3 leaves the minimum TLS version to OpenSSL's defaults, which
        # vary by system and can still allow TLS 1.0/1.1. Set it ourselves.
        context = super().get_ssl_context()
        if isinstance(context, ssl.SSLContext):
            context.minimum_version = ssl.TLSVersion.TLSv1_2
        return context


class EbooksAgent:
    def __init__(self, config: Config | None = None):
        self._config = config or Config()
        self._bot: irc3.IrcBot | None = None
        self._plugin: EbooksPlugin | None = None
        # Every request this session, oldest first, and the task doing each
        self._requests: dict[str, Request] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._ids = itertools.count(1)
        # One queue per events() stream
        self._subscribers: set[asyncio.Queue[Event]] = set()
        self._state = ConnectionState.STOPPED
        # Set once stop() begins; the agent can't be started again after it
        self._stopped = False
        # The server we're in, for status()
        self._peer: str | None = None
        self._fingerprint: str | None = None
        # A MembersChanged waiting to go out (see MEMBERS_INTERVAL)
        self._members_timer: asyncio.TimerHandle | None = None

    def _build_bot(self) -> irc3.IrcBot:
        # Configure our server / user settings
        irc = self._config.irc
        bot = _IrcBot.from_config({
            "nick": irc.nick,
            # Shown by /whois; irc3's own defaults would say we're a bot
            "username": irc.username,
            "realname": irc.realname,
            "autojoins": [],
            "host": irc.host,
            "port": irc.port,
            "ssl": True,
            # Encrypt without verifying — see the note at the top of this file
            "ssl_verify": "CERT_NONE",
            "includes": [
                "irc3.plugins.core",
                "irc3.plugins.userlist",
            ],
            # Read by EbooksPlugin, which irc3 creates for us
            "ebooks_config": self._config,
        })
        bot.include(EbooksPlugin)
        plugin = bot.get_plugin(EbooksPlugin)
        self._attach(plugin)
        # Add our event handlers
        # 1. Listening for a connection
        # 2. Listening for the end of the channel's member list (we're in)
        # 3. Listening for people joining and leaving
        # 4. Logging every message addressed to us
        # 5. Listening for a DCC file send
        bot.attach_events(
            irc3.event(irc3.rfc.CONNECTED, plugin.on_connected),
            irc3.event(irc3.rfc.RPL_ENDOFNAMES, plugin.on_names_end),
            irc3.event(irc3.rfc.JOIN_PART_QUIT, plugin.on_membership),
            irc3.event(irc3.rfc.KICK, plugin.on_membership),
            irc3.event(irc3.rfc.NEW_NICK, plugin.on_membership),
            irc3.event(
                # Any PRIVMSG/NOTICE. The prefix is optional and needn't be a
                # nick!user@host, so server notices are caught too.
                r"^(@(?P<tags>\S+) )?(:(?P<mask>\S+) )?(?P<command>PRIVMSG|NOTICE) (?P<target>\S+) :(?P<text>.*)",
                plugin.on_message,
            ),
            irc3.event(
                # Mirrors irc3's own PRIVMSG pattern: optional IRCv3 tags, then the
                # ":" prefix marker *outside* the mask group so the nick parses as
                # "Search", not ":Search"
                r"^(@(?P<tags>\S+) )?:(?P<mask>\S+!\S+@\S+) (?:PRIVMSG|NOTICE) (?P<target>\S+) :(?P<text>.*DCC SEND.*)",
                plugin.on_dcc_send,
            ),
        )
        # Return a reference to our bot
        return bot

    async def start(self):
        """Start connecting, and return without waiting for it (see wait_ready()).

        Requests made in the meantime wait in PENDING. Can only be called once.
        """
        if self._bot is not None or self._stopped:
            raise RuntimeError("An agent can only be started once")
        self._bot = self._build_bot()
        self._plugin = self._bot.get_plugin(EbooksPlugin)
        self._plugin.downloads.create_dirs()
        self._set_state(ConnectionState.CONNECTING)
        self._bot.create_connection()

    async def wait_ready(self, timeout: float | None = None):
        """Wait until we're in the channel. Raises TimeoutError after `timeout` seconds."""
        plugin = self._require_plugin()
        try:
            async with asyncio.timeout(timeout):
                await plugin.ready.wait()
        except TimeoutError:
            irc = self._config.irc
            raise TimeoutError(
                f"Could not connect to {irc.host}:{irc.port} within {timeout:g}s"
            ) from None

    def _attach(self, plugin: EbooksPlugin):
        # Turn what the plugin hears into events
        plugin.on_status = self._on_status
        plugin.on_bot_message = self._on_bot_message
        plugin.on_members_changed = self._on_members_changed

    def _on_status(self, state: ConnectionState):
        if self._stopped:
            return  # e.g. the server hanging up after our QUIT
        if state is ConnectionState.READY:
            self._read_tls()
            if self._bot is not None:
                self._bot.joined()
        else:
            self._peer = self._fingerprint = None
        self._set_state(state)

    def _set_state(self, state: ConnectionState):
        if state is not self._state:
            self._state = state
            self._emit(StatusChanged(self.status()))

    def _on_members_changed(self):
        if self._members_timer is None and not self._stopped:
            loop = asyncio.get_running_loop()
            self._members_timer = loop.call_later(MEMBERS_INTERVAL, self._emit_members_changed)

    def _emit_members_changed(self):
        self._members_timer = None
        self._emit(MembersChanged())

    def _read_tls(self):
        # Record which server we landed on and its certificate fingerprint,
        # so a change is at least visible in the logs and in status()
        transport = getattr(getattr(self._bot, "protocol", None), "transport", None)
        if transport is None:
            return
        tls = transport.get_extra_info("ssl_object")
        peer = transport.get_extra_info("peername")
        der = tls.getpeercert(binary_form=True) if tls else None
        self._peer = peer[0] if peer else None
        self._fingerprint = hashlib.sha256(der).hexdigest()[:16] if der else None
        log.info("TLS %s to %s (cert sha256 %s…, unverified)",
                 tls.version() if tls else "?", self._peer, self._fingerprint or "unknown")

    def _on_bot_message(self, nick: str, text: str, request_id: str | None):
        # The plugin links it the way it matches offers: we pass each
        # request's id to the plugin as its key
        self._emit(BotMessage(nick, text, request_id))

    async def stop(self, timeout: float = QUIT_TIMEOUT):
        """Cancel outstanding requests, then send QUIT and wait (up to
        `timeout`) for the server to close the connection. For good: every
        events() stream ends, and the agent can't be started again."""
        if self._stopped:
            return
        self._stopped = True
        try:
            for r in self.requests():
                self.cancel(r.id)
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
            if self._members_timer:
                self._members_timer.cancel()
            if self._bot is not None:
                self._bot.stopped = True  # no more (re)connecting
                await self._quit(timeout)
        finally:
            self._state = ConnectionState.STOPPED
            self._peer = self._fingerprint = None
            self._emit(StatusChanged(self.status()))
            for queue in self._subscribers:
                queue.put_nowait(_END)
            self._subscribers.clear()

    async def _quit(self, timeout: float):
        protocol = getattr(self._bot, "protocol", None)
        if protocol is None or protocol.closed:
            return  # never connected, or already gone
        # irc3 reconnects whenever a connection it didn't close drops. The
        # server is about to drop this one on purpose, so mark it closed first.
        protocol.closed = True
        try:
            async with asyncio.timeout(timeout):
                # send_line's future resolves once the line is written out
                await self._bot.send_line("QUIT :bye")
                await self._plugin.disconnected.wait()
            log.info("Disconnected from %s", self._config.irc.host)
        except TimeoutError:
            log.warning("Server didn't close the connection within %gs of QUIT", timeout)
        finally:
            protocol.transport.close()

    def _require_plugin(self) -> EbooksPlugin:
        if self._stopped:
            raise RuntimeError("The agent has stopped")
        if self._plugin is None:
            raise RuntimeError("Not started — call start() first")
        return self._plugin

    # --- state ------------------------------------------------------------

    def status(self) -> AgentStatus:
        irc = self._config.irc
        return AgentStatus(
            state=self._state,
            server=irc.host,
            nick=self._bot.nick if self._bot else irc.nick,
            channel=irc.channel,
            peer=self._peer,
            tls_fingerprint=self._fingerprint,
        )

    def requests(self) -> tuple[Request, ...]:
        """Every request this session, oldest first, finished ones included."""
        return tuple(self._requests.values())

    def members(self) -> frozenset[str]:
        """Who's in the channel, lower-cased. Empty unless READY."""
        return self._plugin.members() if self._plugin and not self._stopped else frozenset()

    # --- commands ---------------------------------------------------------
    #
    # No catch-all timeout on either command: each stage has its own limit.
    # The plugin waits for an offer (counted from when the line is sent), and
    # the transfer has connect and stall timeouts, so a slow but healthy
    # download isn't cut off partway through.

    def search(self, query: str) -> Request:
        """Start an `@search <query>`; the results file is saved in the searches dir."""
        plugin = self._require_plugin()
        query = check_query(query)
        return self._start(RequestKind.SEARCH, f"@search {query}", None,
                           lambda update, key: plugin.search(query, update, key))

    def request(self, line: str) -> Request:
        """Start a `!Bot <file>` request; the book is saved in the downloads dir."""
        plugin = self._require_plugin()
        line, bot = check_request(line)
        return self._start(RequestKind.BOOK, line, bot,
                           lambda update, key: plugin.request(line, bot, update, key))

    def cancel(self, request_id: str) -> None:
        """Stop waiting for or receiving a request. No-op if it already finished."""
        task = self._tasks[request_id]
        if not self._requests[request_id].state.finished:
            task.cancel()
            self._update(request_id, state=RequestState.CANCELLED)

    async def wait(self, request_id: str) -> Request:
        """Wait until a request finishes and return its final snapshot."""
        await asyncio.wait({self._tasks[request_id]})
        return self._requests[request_id]

    def _start(self, kind: RequestKind, line: str, bot: str | None, work) -> Request:
        now = time.time()
        req = Request(id=str(next(self._ids)), kind=kind, line=line, bot=bot,
                      state=RequestState.PENDING, created_at=now, updated_at=now)
        self._set(req)
        self._tasks[req.id] = asyncio.create_task(self._run(req.id, work))
        return req

    async def _run(self, request_id: str, work):
        """Do a request's work, recording how it ends. Cancellation is recorded by cancel()."""
        try:
            path = await work(lambda **changes: self._update(request_id, **changes), request_id)
        except (TimeoutError, ConnectionError, DownloadError) as e:
            self._update(request_id, state=RequestState.FAILED, error=str(e))
        except Exception:
            log.exception("%r failed unexpectedly", self._requests[request_id].line)
            self._update(request_id, state=RequestState.FAILED,
                         error="internal error (see agent log)")
        else:
            self._update(request_id, state=RequestState.DONE, path=path)
            # The book is complete under its final name. DONE first: the hook
            # is ours, not the request's, so cancel() no longer applies, but
            # it's still in this task, so stop() and wait() wait for it.
            dl = self._config.downloads
            if dl.on_complete and self._requests[request_id].kind is RequestKind.BOOK:
                try:
                    await run_hook(dl.on_complete, path, dl.on_complete_timeout)
                except Exception:
                    log.exception("Hook failed unexpectedly for %s", path)

    def _update(self, request_id: str, **changes):
        old = self._requests[request_id]
        if old.state.finished:
            return  # e.g. a last progress report racing a cancel
        now = time.time()
        if (changes.keys() == {"received"} and changes["received"] != old.size
                and now - old.updated_at < PROGRESS_INTERVAL):
            return
        self._set(dataclasses.replace(old, updated_at=now, **changes))

    def _set(self, req: Request):
        self._requests[req.id] = req
        self._emit(RequestUpdated(req))

    # --- events -----------------------------------------------------------

    def events(self) -> AsyncIterator[Event]:
        """A new, independent stream of events from now on. aclose() it to unsubscribe.

        It ends once the agent has stopped (at once, if it already has).
        """
        return _EventStream(self._subscribers, ended=self._stopped)

    def _emit(self, event: Event):
        # Unbounded queues: events are small and progress is rate-limited
        for queue in self._subscribers:
            queue.put_nowait(event)


# Queued by stop() to end each stream
_END = object()


class _EventStream:
    """One subscriber's events. Subscribed on creation, unsubscribed by aclose().

    A class rather than an async generator: a generator's cleanup never runs
    if it's closed before its first item, which would leave the queue
    subscribed and filling up forever.
    """

    def __init__(self, subscribers: set[asyncio.Queue[Event]], ended: bool = False):
        self._subscribers = subscribers
        self._queue: asyncio.Queue[Event] = asyncio.Queue()
        if ended:
            self._queue.put_nowait(_END)
        else:
            subscribers.add(self._queue)

    def __aiter__(self):
        return self

    async def __anext__(self) -> Event:
        event = await self._queue.get()
        if event is _END:
            self._queue.put_nowait(_END)  # and on every later call
            raise StopAsyncIteration
        return event

    async def aclose(self):
        self._subscribers.discard(self._queue)
