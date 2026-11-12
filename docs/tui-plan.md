# TUI plan

A multi-mode, keyboard-driven terminal app for searching `#ebooks`, browsing past searches and requesting books. Inspired by Neovim: a netrw/neo-tree style sidebar, specialized buffers, and modal key bindings.

## Background

Today the agent runs headless in the foreground, logging as it goes, and takes commands over a Unix socket. The TUI is one more frontend over the same agent: you search with `@search <query>`, look back through past searches, open one to see its results, and press Enter on a result to request it.

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
- **Not every bot follows the format.** Real searches showed four variants: two spaces before `::INFO::`, sometimes with `::HASH:: <hex>` after the size (Firebound, Ook, peapod, Pondering-Ebooks2, Wench); no `::INFO::` and no size at all (Horla, Dumbledore); and Ashurbanipal's `<id> - Author - Title [eng]  (PDF) 44.7 MB - [tags]`. At first only the standard format was parsed, which dropped nearly half the lines of some searches. **Decision (revised):** every `!<bot> …` line is a result. `::INFO::` onwards, or a trailing `(EXT) size - [tags]`, are details; the rest is the request. Ids before the name (`hash |`, `%HASH%`, Ashurbanipal's) stay in the request but not the title. Anything else after the header is kept verbatim in `SearchResults.unparsed`, and the original file stays on disk.
- **Many results are `.rar`** (526 of 1,763 across the first real searches), often with the real format in the name: `Title (epub).rar`. The agent only extracts `.zip`, so a `.rar` is saved as it is.
- **Some bots may be offline.** The header warns that a request to a bot not in the channel does nothing. The agent's `members()` says who's in the channel, so the TUI can grey out those results.
- Files use CRLF line endings and are ASCII. The search bot caps results at 1,000.

## Layout

An early sketch, not a spec: the stages below decide what actually gets built, by using each one.

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

Also a sketch. For now there's no mode system: whichever pane has focus decides what keys do, and a text box is effectively insert mode. Keys are added only as features need them (see *Stages*).

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

All bindings live in **one keymap table**, so they're easy to change or later load from config. *(Open question 2.)*

## Architecture

### Framework: Textual

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
    async def start(self) -> None: ...                     # returns at once; then watch StatusChanged
    async def wait_ready(self, timeout=None) -> None: ...  # until the channel is joined
    async def stop(self) -> None: ...                      # for good; event streams end
    def search(self, query: str) -> Request: ...           # returns immediately; work continues in the background
    def request(self, line: str) -> Request: ...           # same, for a "!Bot ..." line
    def cancel(self, request_id: str) -> None: ...
    async def wait(self, request_id: str) -> Request: ...  # the final snapshot
    def events(self) -> AsyncIterator[Event]: ...          # StatusChanged, RequestUpdated, BotMessage, MembersChanged
    def status(self) -> AgentStatus: ...                   # connection state, server, nick, TLS
    def requests(self) -> tuple[Request, ...]: ...         # every request this session
    def members(self) -> frozenset[str]: ...               # who's in the channel (offline bots greyed out)
```

- **Phase 1:** `AgentService` is the agent object itself, running in the TUI's process. The Unix socket keeps running there too, as it does in the headless agent today.
- **Later (optional):** run the agent as a daemon and have the TUI connect to it over the Unix socket as a client, so one IRC connection serves the TUI and scripts. That needs more of the interface over the socket (see *Transports* in `core-interface.md`), so it's only worth building once it's actually needed. *(Open question 1.)*

Today's headless mode (logs to the terminal, input over the socket) stays available, for example as `ebooks_agent.py --headless`, with the TUI as the default.

## Stages

Built in thin slices, each one usable on its own. Using each stage decides the next, so later stages are rough on purpose. The TUI runs from `ebooks_tui.py` until it's ready to be the default.

| Stage | Scope | Status |
|---|---|---|
| 1 | Browse past searches from disk: searches list (newest first), results table, `j`/`k`, `Enter`/`l` in, `h`/`Esc` back, `q` quit. No IRC | done |
| 2 | Go live: the TUI starts the agent and the socket, the status bar shows the connection, `Enter` on a result requests it. A state column on each row (`…`, `42%`, `✓`, `✗`) and the status bar (progress, bot replies, the saved path or the error) follow it, including requests made over the socket. `q` asks first if something's in flight | done |
| 3 | Search from the TUI: `s` opens a one-line `@search` prompt (Enter sends, Esc cancels, a rejected query keeps it open with the reason). The search appears at the top of the sidebar at once, following its state (`queued`, `searching…`, `receiving 40%`), including searches made over the socket. Its results always open when they arrive, without taking focus from the prompt. A failed search stays in the sidebar with `✗` and its reason: Enter runs it again, `d` dismisses it | done |
| — | Delete past searches: `d` on one in the sidebar asks `[y/n]` in place of the search prompt, and only `y` deletes its file (any other key, or focus moving away, says no; keys don't reach anything else meanwhile) | done |
| 4 | Filter results: `/` narrows the table as you type, `Esc` clears | |
| 5 | See what's happening: transfers (with cancel), downloads, the log. Where they live is decided by stages 2–4 | |
| 6 | Polish from use: ~~offline bots greyed out~~ (done: a bot not in the channel has its rows dimmed with `○`, live, and the subtitle names them; Enter on one is refused; while not joined nobody counts as offline), sorting, `?` help, `gg`/`G`, the TUI as the default with `--headless` | |

Deferred until something needs them: `:` commands, a mode system, several buffers or tabs, keymaps from config, the daemon split.

Testing: each stage is tested headless with Textual's `run_test()` (`tests/test_tui.py`), against a fake `AgentService` from stage 2 on, plus one live check.

## Open questions

1. **In-process or daemon from the start?** The recommendation is in-process first. Are there other scripts that need the same IRC connection at the same time as the TUI?
2. **Key bindings.** Strict Vim (hjkl, `:` commands) with arrow keys also working, or closer to neo-tree's defaults?
3. **Search history.** Every search is kept, and a repeat gets a new file; `d` deletes one. Should the sidebar also cap what it shows (for example the last 50)?
