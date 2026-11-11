# Core interface

The contract between the IRC/DCC core and anything that drives it. The types live in [`core.py`](../core.py); `EbooksAgent` in [`agent.py`](../agent.py) implements it.

## Why this exists

The agent started as `search(term) -> Path`: send a line, wait, return a file. That's how a function call works, not how IRC works:

- **It's two steps.** `@search` returns a results file, and then `!Bot …` returns the book.
- **Replies arrive when the bot gets to them.** Bots may answer with notices ("searching…", "file not found") before any offer.
- **Several requests can be in flight**, one per bot or more.

The global search lock and the fixed 60s timeout were symptoms of that mismatch.

The interface is also **frontend-agnostic on purpose**. We haven't decided between:

- **A**, a TUI that runs the agent in its own process;
- **B**, a long-running daemon that a TUI, scripts and small CLI commands connect to over a socket.

The core must work unchanged for both. Choosing B later should mean writing a transport, not redesigning the core.

## Shape

**Commands in, events out, state on request.**

| | Call | Returns |
|---|---|---|
| Lifecycle | `await start()`, `await stop()` | — |
| Commands | `search(query)`, `request(line)` | a `Request` snapshot **immediately**; the work continues in the background |
| | `cancel(request_id)` | — |
| | `await wait(request_id)` | the final snapshot (a convenience for scripts) |
| State | `status()`, `requests()` | snapshots of the connection and of every request this session |
| Events | `events()` | a new, independent async stream per caller |

### Requests

A `Request` is an immutable snapshot. Every change produces a new one, delivered as `RequestUpdated`.

```
PENDING ──▶ WAITING ──▶ TRANSFERRING ──▶ DONE

  (any non-finished state) ──▶ FAILED / CANCELLED
```

- `PENDING`: accepted, not sent yet (still connecting, or another search is in flight).
- `WAITING`: sent to the channel, no offer yet.
- `TRANSFERRING`: offer accepted, bytes arriving. `received` and `size` give progress.
- `DONE` sets `path`, and `FAILED` sets `error`.

### Events

| Event | When |
|---|---|
| `StatusChanged` | joined the channel, or disconnected (`reason` says which) |
| `RequestUpdated` | any change to a request, including progress (at most 4 a second per transfer) |
| `BotMessage` | a NOTICE or PRIVMSG sent to us by another user, linked by the same rule as offers: the oldest request waiting on that bot, else the search waiting for its offer |

This deliberately replaces the plan's longer list (`OfferAccepted`, `Progress`, `Completed`, `Failed`, …) with one `RequestUpdated` that carries the whole snapshot. Frontends re-render the row instead of reconstructing state from deltas, and a client that missed events can call `requests()` to catch up.

### Rules that keep it agnostic

- Everything that crosses the boundary is a frozen dataclass of plain values. `to_dict()` turns it into JSON. `tests/test_core.py` enforces both.
- No callbacks into frontends, and no asyncio objects in payloads.
- Commands never block on IRC. Only `start`, `stop` and `wait` are async.
- Logging stays separate. Log lines are for humans; events are for programs.

## Decisions

Settled on 2026-11-09.

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

## Other decisions

1. **History.**
   - `requests()` covers this session only.
   - Past searches already persist as files in `searches/`, and books in `downloads/`.
   - Persisting requests themselves, so queued requests survive a restart, only matters in a daemon. Left out for now.
2. **Slow subscribers.** Each `events()` stream has its own unbounded queue. Events are small and progress is rate-limited, so a bounded queue with a drop policy isn't worth it yet.

## How Phase 0 maps onto it

| TODO item | Became |
|---|---|
| 0.2 Separate `search()` and `request()` | the two commands, with `RequestKind`; results files saved to `searches/` |
| 0.3 Implement the core interface | `EbooksAgent`: `events()`, `status()`, `requests()`, `cancel()`, `wait()` and the three event types |
| 0.4 Progress reporting | `Downloads.receive(on_progress=…)` feeding `RequestUpdated` |
| 0.5 Per-bot tracking | the plugin's list of waiting requests, with offer matching per decision 1 |
| 0.6 Logging | unchanged, and separate from events |
| 0.7 Rebuild the Unix socket | a JSON-lines protocol streaming each request's events (`to_dict`), the first transport over the core |
