# TUI plan

A multi-mode, keyboard-driven terminal app for searching `#ebooks`, browsing past searches and requesting books. Inspired by Neovim: a netrw/neo-tree style sidebar, specialized buffers, and modal key bindings.

## Background

The project started with an interactive prompt. The Unix socket came next, so other processes could drive the agent, and it later replaced the prompt entirely: the agent now runs headless in the foreground, logging as it goes. The TUI is one more frontend over the same agent: you search with `@search <query>`, look back through past searches, open one to see its results, and press Enter on a result to request it.

## The data

A search returns a zipped `.txt` file. A real one (`@search warhammer.txt`) has 5 header lines followed by 1,000 result lines:

```
Search results from SearchBot v3.00.13 by Ook, ...
Searched 14 lists for "warhammer" , found 1000 matches. Enjoy!
This list includes results from ALL the lists SearchBot v3.00.13 currently has, some of these servers may be offline.
Always check to be sure the server you want to make a request from is actually in the channel, otherwise your request will have no effect.
For easier searching, use sbClient script ...

!Bsk Dan Abnett - Warhammer 40,000 - Penitent (Retail).epub ::INFO:: 2.19MB
!artemis_serv f73222c7950c | Warhammer 40K_ The Purging of Kadillus - Gav Thorpe.mobi ::INFO:: 5.09MB
!Ashurbanipal wLzfYssqwXcipxVs8UQztw - Games-Workshop - ... [eng]  (PDF) 44.7 MB - [Fantasy, ...]
```

Observations (from the sample: 996 Bsk, 2 artemis_serv, 2 Ashurbanipal; 856 epub, 124 mobi, 18 azw3):

- **The request is easy to find.** Everything before ` ::INFO::` is the exact line to send back, and the first word (`!Bsk`) is the bot that will reply. This holds for 998 of the 1,000 lines.
- **Size and file type can be parsed** (`2.19MB`, `680.86KB`, `44.7 MB`, plus the file extension), so results can be shown in columns, sorted and filtered.
- **One bot doesn't follow the format.** Ashurbanipal lines have no `::INFO::` marker (`(PDF) 44.7 MB - [tags]`). **Decision:** only the standard format is parsed; other lines aren't shown as results, and are kept verbatim in `SearchResults.unparsed`. The original file stays on disk for auditing. Dropping one provider is better than maintaining a guessed special case.
- **Some bots may be offline.** The header warns that a request to a bot not in the channel does nothing. The agent already tracks channel membership (irc3 userlist), so the TUI can grey out those results.
- Files use CRLF line endings and are ASCII. The search bot caps results at 1,000.

## Layout

```
┌ SEARCHES ───────────────┬ results: warhammer ── 1000 matches · 14 lists ─────────────┐
│ ▸ warhammer      1000   │  BOT    TITLE                                     TYPE  SIZE│
│   dune             84   │▶ Bsk    Dan Abnett - Warhammer 40,000 - Penitent  epub  2.2M│
│   foundation       31   │  Bsk    Danie Ware - The Rose in Darkness          epub  681K│
│ DOWNLOADS               │  ░artemis_serv  Fall of Damnos (offline)          mobi  4.7M│
│   Tomb World.epub  2.1M │  ...                                                        │
│ TRANSFERS               │                                                             │
│   ⇣ Penitent  ▓▓▓░ 64%  │                                                             │
├─────────────────────────┴─────────────────────────────────────────────────────────────┤
│ NORMAL │ warhammer [/epub] 856/1000 │ Bsk: "You are in queue position 2" │ ● TLS 142.44… │
└──────────────────────────────────────────────────────────────────────────────────────────┘
```

The sidebar on the left works like netrw/neo-tree. The main pane shows the current buffer. The status line at the bottom shows the mode, the current buffer and filter, the latest bot notice, and the connection state.

### Buffers

| Buffer | Contents | Enter does |
|---|---|---|
| Searches | Past searches, read from `searches/` | Open its results buffer |
| Results | Parsed result lines from one search | Send the `!Bot …` request |
| Downloads | Finished books in `downloads/` | Show the path, or open the file with `xdg-open` |
| Transfers | Pending, waiting, in-progress and failed requests | Retry or cancel |
| Log | IRC notices, warnings, rejected offers | — |

### Modes and keys

- **Normal** (the default)
  - `j`/`k` or the arrow keys: move up and down
  - `Enter`: act on the selected row
  - `h`/`l`: move between the sidebar and the main pane
  - `q`: close the buffer
  - `gg`/`G`: jump to the top or bottom
- **Search prompt**
  - `s`, or `:search <query>`: open the input; Enter sends `@search <query>`
- **Filter**
  - `/`: narrow the current buffer as you type (a filter, not a jump)
  - `n`/`N`: step through matches
  - `Esc`: clear the filter
- **Command**
  - `:` for `:search`, `:sort size|bot|name`, `:filter epub`, `:q`, `:help`

All bindings live in **one keymap table**, so they're easy to change or later load from config. *(Open question 3.)*

## Architecture

### Framework: Textual (recommended)

[Textual](https://textual.textualize.io/):

- It's **built on asyncio**, so the TUI, irc3 and the Unix socket server all share one event loop, the same way the current CLI works. That's the deciding factor.
- It ships the widgets we need: a list view, data table, tree, text input, progress bar and status bars, styled with CSS.
- It has **headless testing** (`App.run_test()` and the `Pilot` API), so the UI is testable the same way the rest of the project is.

Alternatives considered:

- **prompt_toolkit** has genuine vi-mode text editing but lower-level layout tools.
- **urwid** and **curses** would mean building most of the widgets ourselves.

### In-process first, with a seam for a daemon

The TUI talks to an `AgentService` interface, defined in `core.py` and described in [`core-interface.md`](core-interface.md). In short:

```python
class AgentService(Protocol):
    def search(self, query: str) -> Request: ...           # returns immediately; work continues in the background
    def request(self, line: str) -> Request: ...           # same, for a "!Bot ..." line
    def cancel(self, request_id: str) -> None: ...
    async def wait(self, request_id: str) -> Request: ...  # the final snapshot
    def events(self) -> AsyncIterator[Event]: ...          # StatusChanged, RequestUpdated, BotMessage
    def status(self) -> AgentStatus: ...                   # connected?, server, nick, TLS
    def requests(self) -> tuple[Request, ...]: ...         # every request this session
```

- **Phase 1:** `AgentService` is the agent object itself, running in the TUI's process. The Unix socket keeps running there too, as it does in the headless agent today.
- **Later (optional):** run the agent as a daemon and have the TUI connect to it over the Unix socket as a client. Then one IRC connection serves the TUI and scripts. The socket already speaks JSON lines and streams each request's events (see `unix_socket.py`). A client needs more than that, though: a subscription to every event, and `requests()` and `cancel()`. So it's only worth building once it's actually needed. *(Open question 2.)*

Today's headless mode (logs to the terminal, input over the socket) stays available, for example as `ebooks_agent.py --headless`, with the TUI as the default.

## Backend prerequisites (phase 0)

These come before any UI work, and are done. They're tracked in [`TODO.md`](../TODO.md#phase-0--tui-backend-prerequisites), and the interface they built is specified in [`core-interface.md`](core-interface.md): the results parser, separate `search()` and `request()`, the event stream, progress reporting, per-bot request tracking, all output through logging, and the Unix socket rebuilt on the core.

Two plans from earlier drafts were dropped along the way: a `QUEUED` state with no offer timeout (bot queues have only lasted seconds), and restarting the offer timeout whenever a bot speaks.

## Phases

| Phase | Scope | Testable without the network? |
|---|---|---|
| 0 | Backend prerequisites above | ✅ unit tests |
| 1 | TUI skeleton: layout, status line, Searches sidebar read from disk, read-only Results buffer, `j`/`k`/`Enter`/`q` | ✅ Textual Pilot |
| 2 | Search prompt connected to `search()`; the new search appears in the sidebar and opens automatically | ✅ fake `AgentService` |
| 3 | Enter on a result sends `request()`; Transfers buffer with progress; Downloads buffer | ✅ fake service, plus one live check |
| 4 | Filter and sort, `:` commands, help buffer, offline bots greyed out, bot notices in the status line | ✅ |
| 5 (optional) | Daemon and client split over the Unix socket with a JSON protocol | ✅ |

## Open questions

1. ~~**Ashurbanipal request format.**~~ Decided: non-standard lines are skipped (see *The data*).
2. **In-process or daemon from the start?** The recommendation is in-process first. Are there other scripts that need the same IRC connection at the same time as the TUI?
3. **Key bindings.** Strict Vim (hjkl, `:` commands) with arrow keys also working, or closer to neo-tree's defaults?
4. **Search history.** Keep every search forever or cap it (for example at the last 50)? Should re-running a query replace its file or add a new entry?
