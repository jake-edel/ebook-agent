# TODO

Outstanding work, roughly in priority order. Security hardening and download reliability are done — see [Done](#done) at the bottom.

## Phase 0 — TUI backend prerequisites

The groundwork for the planned TUI. [`docs/core-interface.md`](docs/core-interface.md) defines the interface these items build (types in `core.py`), and [`docs/tui-plan.md`](docs/tui-plan.md) has the TUI design. Each item is useful even without the TUI.

- [x] **0.1 Results parser**: `results.py`
  - `load_results(path)` / `parse_results(text)` return a `SearchResults` (query, lists searched, match count, header, `items`, `unparsed`). Each `ResultItem` has `bot`, `request`, `title`, `ext`, `size_bytes`, `raw` and `line_no`.
  - Only the standard `!bot name ::INFO:: size` format is parsed. Any other line (so far only Ashurbanipal's) is deliberately left out of the results and kept verbatim in `unparsed`; the original file stays on disk for auditing.
  - Checked against the full real `@search warhammer.txt`: 998 results, with every size and extension found, plus 2 Ashurbanipal lines in `unparsed`.
  - Tests in `tests/test_results.py`, using a trimmed copy of the real file in `tests/fixtures/`.

- [x] **0.2 Separate `search()` and `request()`**: `agent.py`, `plugin.py`
  - `search(query)` sends `@search <query>` and saves the results to `searches/<timestamp> <query>.txt`, unzipped and without the `@search ` prefix.
  - `request(line)` sends a `!Bot …` line, accepts an offer only from that bot, and saves the book to `downloads/` under the bot's filename (or the name inside the zip).
  - Both refuse input containing CR, LF or NUL (IRC command injection) before sending anything, and `request()` refuses anything that isn't `!<bot> <file>`.
  - A `/` in a search query becomes `_` in the filename, so `AC/DC` isn't cut down to `DC`.
  - The Unix socket routes `@search …` and bare text to `search()`, and `!Bot …` to `request()`. Tests in `tests/test_routing.py`.

- [x] **0.3 Implement the core interface**: `agent.py`
  - `EbooksAgent` implements `AgentService`. `search()` and `request()` validate, then return a `PENDING` snapshot at once, with the work running as a task. `cancel()`, `wait()`, `status()`, `requests()` and `events()` are there too, and `stop()` cancels outstanding requests before QUIT.
  - Events: `StatusChanged` on join and disconnect, `RequestUpdated` on every change (progress at most 4 a second), and `BotMessage` for messages sent to us, linked to a request when possible.
  - Kept simple on purpose: no `QUEUED` state, and unbounded per-subscriber queues.
  - Tests in `tests/test_service.py`.

- [x] **0.4 Progress reporting**: `dcc.py` `Downloads.receive()`
  - `receive(..., on_progress=None)` calls `on_progress(received)` after every chunk. The caller already knows the total, and rate-limiting is left to the core.

- [x] **0.5 Track requests per bot instead of one global lock**: `plugin.py`
  - A list of lines waiting for an offer replaces the single pending slot. An offer goes to the oldest open request naming its sender (bots serve requests in order), otherwise to the search in flight, otherwise it's dropped as unsolicited.
  - Book requests to different bots run in parallel. Searches still go one at a time, because any channel member can answer one.
  - Each request keeps the plain offer timeout (`downloads.offer_timeout`, 60s), counted from when its line is sent. Queue detection and restarting the timer on bot messages were dropped: queues have only ever lasted a few seconds.
  - The existing per-offer checks are unchanged.

- [x] **0.6 All output through logging**
  - All status output goes through `logging`. The only `print`s left are startup errors to stderr in `ebooks_agent.py`, before any UI exists.
  - The log handler that feeds the TUI's Log buffer is part of TUI phase 1, not a backend prerequisite.

- [x] **0.7 Rebuild the Unix socket on the core**: `unix_socket.py`
  - JSON lines: one command per connection (`search`, `request`, `status`), answered with that request's `RequestUpdated` and `BotMessage` events (`core.to_dict`) until it finishes, or with an `Error`. The protocol is described in the module docstring and the README.
  - Input limits: 10s to send the command, 8 KiB per command line. The line sent to IRC is capped at 350 bytes in `check_query` / `check_request`, so the TUI gets the same limit (irc3 would otherwise split a long line into several messages).
  - Same access rules: private directory, mode 0600, same user only.
  - Shutdown no longer hangs while a client is waiting on a request (`serve_forever()` waited for every client to hang up).
  - `ebooks_agent.py --socket-path` prints the socket's location, and the Neovim `:Ebook` command uses it instead of guessing. `:Ebook` now speaks the new protocol, shows bot replies and progress, and sends a selected `!Bot … ::INFO:: …` results line as a request.
  - Fixed along the way: closing an `events()` stream before reading from it left it subscribed.
  - Tests in `tests/test_socket.py` and `tests/test_main.py`.

- [x] **Backend cleanup before the TUI**
  - TLS 1.2 is the minimum, set in our code (`_IrcBot`) rather than left to the system's OpenSSL defaults.
  - A zip that can't be extracted is deleted, and one holding several files is kept (only the best ebook is extracted).
  - Extraction runs in a thread, so a large zip doesn't stall IRC or a UI.
  - DCC offers: bare filenames with spaces and dotted-quad IPs are accepted. A malformed `DCC SEND` from the bot we're waiting on fails the request with the reason instead of timing out.
  - The plugin reads its config once, from the bot's config. Input is checked once, in `EbooksAgent`. Offers and bot messages are matched to requests by one rule (`EbooksPlugin._match`).
  - `search.offer_timeout` is now `downloads.offer_timeout`: it applies to book requests too.

- [x] **TUI-facing interface**: `core.py`, `agent.py`
  - `AgentStatus.state` (`stopped`, `connecting`, `ready`, `disconnected`) replaces `connected` and `StatusChanged.reason`.
  - `start()` returns at once, and `wait_ready(timeout)` waits for the join. Requests made in between wait in `PENDING`. `stop()` is final, also after a failed start: `_IrcBot` stops irc3's retries, including an attempt still in progress.
  - After `stop()`, every `events()` stream ends.
  - `members()` and a `MembersChanged` event (at most one per 2s) let the TUI grey out offline bots.
  - `status()` no longer shows the last server's address and fingerprint once disconnected. Our own QUIT no longer logs a "Disconnected" warning.
  - Tests in `tests/test_service.py` and `tests/test_shutdown.py`.

## Connection lifecycle

- [x] **Report "connected" only after the JOIN is confirmed**: the plugin's public `ready` event is set on the end-of-NAMES reply (366) for our channel, so nothing is sent before we're in it and the member list is filled in.
- [x] **Handle disconnects and reconnects**: `ready` is cleared when the connection drops. Lines not sent yet wait for the reconnect, lines waiting for an offer fail with a clear error (the bot's queue entry is gone with our nick), and transfers carry on.

## Robustness / polish

- [ ] **Pin dependencies**
  - **Problem:** `requirements.txt` (`irc3`) and `requirements-dev.txt` (`pytest`) don't specify versions.
  - **Fix:** pin known-good versions (`irc3==1.1.12` at the time of writing), or add a lock file.

- [ ] **irc3 will break on Python 3.16**
  - **Problem:** irc3 1.1.12 calls `asyncio.iscoroutinefunction` (in `irc3/dec.py` and `irc3/utils.py`), deprecated since Python 3.14 and removed in 3.16 (the venv is on 3.13.5 at the time of writing). The warning is currently hidden in `pytest.ini`.
  - **Fix:** watch for an irc3 release that fixes it, or patch it locally, before upgrading Python.

- [ ] **CI**
  - **Fix:** add a GitHub Actions workflow that runs `pytest` on every push.

## Feature ideas

- [ ] **TUI**: the interactive, Neovim-inspired frontend. Design and phases are in [`docs/tui-plan.md`](docs/tui-plan.md), starting with [Phase 0](#phase-0--tui-backend-prerequisites).
- [ ] **Fully automatic `@search` → `!Bot` flow**
  - **Idea:** a non-interactive mode for scripts and the socket: send `@search`, parse the results (Phase 0.1), pick the best match (prefer `.epub`, a bot that's in the channel, a reasonable size), then request it.
  - **Relation to the TUI:** the TUI is the version where you make that choice yourself.

## Known limitations / accepted risks

These were decided on purpose. Revisit them if the threat model changes.

- **The IRC TLS connection isn't verified.** IRCHighway round-robins across 5 or more servers, each with its own self-signed cert, and doesn't publish the list, so pinning would break at random. TLS still protects against passive observers, and each connection logs the server IP and cert fingerprint.
- **DCC transfers are plaintext.** That's how the protocol works. An active attacker on your network could tamper with downloads no matter how the IRC connection is protected.
- **The sender check is "any member of `#ebooks`".** During your search window, any of the roughly 600 channel members can make an offer (to a public IP only, within the size limits).
  - Book requests (`!Bot …`) are already strict: only that bot is accepted.
  - Could tighten further to voiced/op users (`+v`/`@`), or a list of known search bots.
- **The download size limit is 100 MiB** (`downloads.max_size_mib` in the config). Raise it if large illustrated PDFs get refused.
- **Downloaded files aren't checked.** A file named `.epub` could contain anything. Open downloads with appropriate care.

## Done

- [x] **Path traversal:** filenames from bots are cleaned up, kept inside `downloads/` or `searches/`, and symlinks are never followed (`O_NOFOLLOW`).
- [x] **Offer filtering:** offers must be sent directly to our nick, arrive while we're waiting on that sender, come from a channel member (or the bot named in `!Bot`), and point at a public IP on port 1024 or above. Stale offers are no longer queued. Also fixed the `:Search` leading-colon bug in the event pattern.
- [x] **No interactive prompt:** the `search>` prompt is gone. `ebooks_agent.py` runs in the foreground, logs requests and results, takes searches only over the Unix socket, and exits cleanly on Ctrl+C or SIGTERM. It no longer needs stdin, so it runs fine under `nohup`, systemd or `< /dev/null`. If the socket can't be opened, startup fails instead of leaving an agent nothing can reach. Tests in `tests/test_main.py`.
- [x] **Configuration:** `config.py` loads the built-in defaults, overridden by `config.toml` (or the file given with `--config`). Unknown keys, wrong types and out-of-range values stop startup with a clear message. `config.example.toml` is the committed, documented template, and `config.toml` is gitignored. The loaded `Config` is the only copy of each setting at runtime: `dcc.Downloads` and the plugin are handed their settings, with no module-level globals. Tests in `tests/test_config.py`.
- [x] **No module-import side effects:** `dcc.py` no longer creates `downloads/` on import. `Downloads.create_dirs()` creates it at startup.
- [x] **Download reliability:**
  - DCC connects time out after 15s (`downloads.connect_timeout`) and transfers give up after 30s without data (`downloads.stall_timeout`). There's no limit on total time, so slow but steady downloads finish.
  - The outer 60s catch-all in `EbooksAgent.search` is gone. The 60s offer wait only starts once a search has its turn.
  - A sender that hangs up early raises `IncompleteDownloadError` instead of returning a partial file as a success.
  - Downloads and extractions go to a `name.<random>.part` file (`O_EXCL`), which is hard-linked into place under a free name (`Dune (1).epub`) only when complete. Existing files are never overwritten, and a failure only deletes our own `.part` file.
  - Connection errors become `TransferError`, and corrupt, empty or encrypted zips become `BadArchiveError`. Both are `DownloadError`s.
  - Unexpected errors are logged, the socket handler answers `error: internal error`, and it always closes the connection.
  - Tests in `tests/test_reliability.py`.
- [x] **Size limits:** 100 MiB cap, free-disk check, never reads past the advertised size, zip bomb guards (header check plus counted streaming), and partial files are deleted when a download fails.
- [x] **TLS:** IRC runs over port 6697, encrypt-only (see the accepted risks above).
- [x] **Unix socket:** lives in `$XDG_RUNTIME_DIR` (or a private fallback folder), created as 0600, accepts the same user only, and is cleaned up on exit.
- [x] **Clean shutdown:** `EbooksAgent.stop()` is async. It sends `QUIT`, waits up to 2s for the server to hang up, then closes the transport, and marks the connection closed first so irc3 doesn't reconnect. A second Ctrl+C during that wait aborts it. Tests in `tests/test_shutdown.py`.
- [x] **Message logging:** every PRIVMSG and NOTICE addressed to us (DMs, bot replies, DCC offers, server notices) is logged at INFO with its exact text. Channel chatter isn't.
- [x] **Test suite:** `tests/`, run with `pytest`.
- [x] **README:** setup and venv instructions.
