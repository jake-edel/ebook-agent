# Core interface

The contract between the IRC/DCC core and anything that drives it. The types live in [`core.py`](../core.py); `EbooksAgent` in [`agent.py`](../agent.py) implements it.

## Why this shape

IRC isn't a function call, so the interface isn't `search(term) -> Path`:

- **It's two steps.** `@search` returns a results file, and then `!Bot …` returns the book.
- **Replies arrive when the bot gets to them.** Bots may answer with notices ("searching…", "file not found") before any offer.
- **Several requests can be in flight**, one per bot or more.

It's also **frontend-agnostic on purpose**. The TUI runs the agent in its own process for now, but a long-running daemon that a TUI, scripts and small CLI commands all connect to is still an option. The core must work unchanged for both, so choosing the daemon later means writing a transport, not redesigning the core.

## Shape

**Commands in, events out, state on request.**

| | Call | Returns |
|---|---|---|
| Lifecycle | `await start()` | — (returns at once; connecting carries on in the background) |
| | `await wait_ready(timeout)` | — once the channel is joined; raises `TimeoutError` |
| | `await stop()` | — (for good: every event stream then ends) |
| Commands | `search(query)`, `request(line)` | a `Request` snapshot **immediately**; the work continues in the background |
| | `cancel(request_id)` | — |
| | `await wait(request_id)` | the final snapshot (a convenience for scripts) |
| State | `status()`, `requests()` | snapshots of the connection and of every request this session |
| | `members()` | who's in the channel, lower-cased, e.g. to grey out offline bots |
| Events | `events()` | a new, independent async stream per caller |

### Connection

`AgentStatus.state` is one of:

```
STOPPED ──start()──▶ CONNECTING ──joined──▶ READY
                         ▲                    │
                         └──── DISCONNECTED ◀─┘   (retried automatically)

  (any state) ──stop()──▶ STOPPED, for good
```

Requests can be made from `start()` on. Until the channel is joined they wait in `PENDING`. A frontend can draw at once and show the state as it changes, and the headless CLI calls `wait_ready()` so a bad host fails at startup. An agent can't be restarted after `stop()`; make a new one.

### Requests

A `Request` is an immutable snapshot. Every change produces a new one, delivered as `RequestUpdated`.

```
PENDING ──▶ WAITING ──▶ TRANSFERRING ──▶ DONE

  (any non-finished state) ──▶ FAILED / CANCELLED
```

- `PENDING`: accepted, not sent yet (still connecting, another search is in flight, or the bot already has `downloads.max_requests_per_bot` of our requests).
- `WAITING`: sent to the channel, no offer yet.
- `TRANSFERRING`: offer accepted, bytes arriving. `received` and `size` give progress.
- `DONE` sets `path`, and `FAILED` sets `error`.

### Events

| Event | When |
|---|---|
| `StatusChanged` | the connection state changed (see above); carries the new `AgentStatus` |
| `RequestUpdated` | any change to a request, including progress (at most 4 a second per transfer) |
| `BotMessage` | a NOTICE or PRIVMSG sent to us by another user, linked by the same rule as offers: the oldest request waiting on that bot, else the search waiting for its offer |
| `MembersChanged` | someone joined or left the channel; call `members()`. At most one every 2 seconds, since hundreds of members come and go |

After `stop()`, each stream delivers what's queued (the cancelled requests, then `StatusChanged` to `STOPPED`) and ends, so a frontend's listener tasks finish by themselves.

There's one `RequestUpdated` carrying the whole snapshot, rather than an event per step (offer accepted, progress, completed…). Frontends re-render the row instead of reconstructing state from deltas, and a client that missed events can call `requests()` to catch up.

### Rules that keep it agnostic

- Everything that crosses the boundary is a frozen dataclass of plain values. `to_dict()` turns it into JSON. `tests/test_core.py` enforces both.
- No callbacks into frontends, and no asyncio objects in payloads.
- Commands never block on IRC. Only `start`, `wait_ready`, `stop` and `wait` are async.
- Logging stays separate. Log lines are for humans; events are for programs.

### Transports

`unix_socket.py` is the first: one JSON-lines command per connection, answered with that request's events (see the README). A daemon client would need more of the interface over it: a subscription to every event, `requests()` and `cancel()`.

## Decisions

1. **Matching offers to requests.**
   - Book requests name their bot. If several requests to the same bot are in flight, offers are matched in the order the requests were sent, which is how bots serve them.
   - Searches can be answered by any channel member, so two at once are ambiguous. Only one search is in flight at a time, and later ones wait in `PENDING`. Searches are quick, so that costs little.
2. **Timeouts.**
   - `WAITING` keeps the offer timeout (60s by default), counted from when the line is sent.
   - `TRANSFERRING` keeps the connect and stall timeouts.
   - A `QUEUED` state (parsing "queue position N" notices) was planned and dropped: bot queues have only ever lasted a few seconds.
3. **Disconnects.**
   - `PENDING` requests wait for the reconnect.
   - `WAITING` requests fail with a clear error, since the bot's queue entry is gone when our nick drops. They aren't resent automatically, which could double-request from a bot that did keep the spot.
   - Transfers in progress are unaffected, because DCC is a separate connection.
4. **History.**
   - `requests()` covers this session only.
   - Past searches already persist as files in `searches/`, and books in `downloads/`.
   - Persisting requests themselves, so queued requests survive a restart, only matters in a daemon. Left out for now.
5. **Slow subscribers.** Each `events()` stream has its own unbounded queue. Events are small and progress is rate-limited, so a bounded queue with a drop policy isn't worth it yet.
