# TODO

Outstanding work, roughly in priority order. Security hardening and download reliability are done — see [Done](#done) at the bottom.

## Phase 0 — TUI backend prerequisites

The groundwork for the planned TUI. See [`docs/tui-plan.md`](docs/tui-plan.md) for the full design. Each item is useful even without the TUI.

- [x] **0.1 Results parser**: `results.py`
  - `load_results(path)` / `parse_results(text)` return a `SearchResults` (query, lists searched, match count, header, `items`, `unparsed`). Each `ResultItem` has `bot`, `request`, `title`, `ext`, `size_bytes`, `raw` and `line_no`.
  - Only the standard `!bot name ::INFO:: size` format is parsed. Any other line (so far only Ashurbanipal's) is deliberately left out of the results and kept verbatim in `unparsed`; the original file stays on disk for auditing.
  - Checked against the full real `@search warhammer.txt`: 998 results, with every size and extension found, plus 2 Ashurbanipal lines in `unparsed`.
  - Tests in `tests/test_results.py`, using a trimmed copy of the real file in `tests/fixtures/`.

- [ ] **0.2 Separate `search()` and `request()`**: `agent.py`, `plugin.py`, `dcc.py`
  - **What:**
    - `search(query)` sends `@search <query>`, expects the zipped `.txt`, and saves it to `searches/<timestamp> <query>.txt` (no `@search ` prefix).
    - `request(line)` sends a `!Bot …` line and saves the book to `downloads/`.
  - **Replaces** "Better names for extracted files" below.

- [ ] **0.3 Event stream**: `agent.py`, `plugin.py`
  - **What:** an asyncio queue of typed events: `Connected`, `Disconnected`, `BotNotice`, `OfferAccepted`, `Progress`, `Completed`, `Failed`.
  - **Why:** the headless mode just logs them; the TUI renders them.
  - **Also capture** NOTICE and PRIVMSG messages from bots we're waiting on, for queue position and "file not found" messages.

- [ ] **0.4 Progress callback**: `dcc.py` `receive_dcc()`
  - **What:** an optional `on_progress(received, total)` callback, emitted as `Progress` events.
- [ ] **0.5 Track requests per bot instead of one global lock**: `plugin.py`
  - **Problem:**
    - Bots queue requests ("you are in queue position N"), and offers can arrive minutes later.
    - A single global lock with a 60s limit means one slow bot blocks everything.
  - **Fix:**
    - Track a dict of pending requests keyed by the expected sender (the `!Bot` nick, or the search bot for `@search`).
    - For each one, wait for an offer from that sender (long or no timeout, with cancel), and forward that bot's notices as events.
    - Keep the existing per-offer checks: sent directly to us, public IP, size limits.
  - The outer 60s catch-all is already gone, and transfers have their own connect and stall timeouts (`CONNECT_TIMEOUT`, `STALL_TIMEOUT` in `dcc.py`). What's left is the per-bot tracking and a longer offer wait.

- [ ] **0.6 All output through logging**: `plugin.py`, `unix_socket.py`, `ebooks_agent.py`
  - **Problem:** once the TUI owns the terminal, a stray `print` breaks the screen.
  - **Fix:** route all status output through `logging`, and add a handler the TUI can attach for its Log buffer.
  - **Status:** mostly done. Removing the prompt turned the remaining status `print`s into log lines. The only `print`s left are startup errors to stderr in `ebooks_agent.py`. Still to do: the handler the TUI attaches.
  - **Replaces** "Consistent output" below.

## Connection lifecycle

- [ ] **Only report "connected" after the JOIN is confirmed**: `plugin.py` `on_connected()`
  - **Problem:** `_connected` is set right after *sending* `JOIN`, so a fast first search could be sent before we're actually in `#ebooks`.
  - **Fix:** set it on our own JOIN echo (or on the end-of-NAMES reply, 366, for `#ebooks`), which also means the member list is ready for the sender check.

- [ ] **Handle disconnects and reconnects**: `plugin.py`, `agent.py`
  - **Problem:** irc3 reconnects automatically, but:
    - `_connected` is never cleared, so searches made during a disconnect are silently lost;
    - a search waiting on an offer just times out.
  - **Fix:**
    - Clear `_connected` when the connection drops.
    - Fail the pending search with a clear error.
    - Have `search()` wait for the connection to come back, with a timeout, before sending.

- [ ] **Clean shutdown**: `ebooks_agent.py` `_main()`, `agent.py` `stop()`
  - **Problem:** `bot.quit()` isn't waited on and the event loop closes right after, so the `QUIT` message may never be sent.
  - **Fix:** make `stop()` async: send QUIT, wait briefly (or until the server closes the connection), then close the transport.

## Robustness / polish

- [ ] **Pin dependencies**
  - **Problem:** `requirements.txt` (`irc3`) and `requirements-dev.txt` (`pytest`) don't specify versions.
  - **Fix:** pin known-good versions (`irc3==1.1.12` at the time of writing), or add a lock file.

- [ ] **irc3 will break on Python 3.16**
  - **Problem:** irc3 1.1.12 calls `asyncio.iscoroutinefunction` (in `irc3/dec.py` and `irc3/utils.py`), which Python 3.16 removes. The warning is currently hidden in `pytest.ini`.
  - **Fix:** watch for an irc3 release that fixes it, or patch it locally, before upgrading Python.

- [ ] **Unix socket input limits**: `unix_socket.py` `handle()`
  - **Problem:** a client that connects and never sends anything holds a handler open forever. (A line over 64 KiB no longer crashes the handler: it's logged and the connection is closed.)
  - **Fix:** put a timeout on `readline()` and cap the length of a search term. Low risk now that only your own user can connect.

- [ ] **Better names for extracted files**: `dcc.py` `extract_ebook()` — *replaced by [Phase 0.2](#phase-0--tui-backend-prerequisites) for search results*
  - **Problem:** extracted files are named after the raw search term, including any command prefix, for example `downloads/@search warhammer.txt`.
  - **Fix:** strip `@search ` or `!Bot ` prefixes. For real books, consider keeping the name from inside the zip (after `safe_download_path`).

- [ ] **Encapsulation**: `agent.py`
  - **Problem:** `agent.py` reaches into the plugin's private `_connected` attribute.
  - **Fix:** expose a public `wait_connected()` method on the plugin.

- [ ] **CI**
  - **Fix:** add a GitHub Actions workflow that runs `pytest` on every push.

## Feature ideas

- [ ] **TUI**: the interactive, Neovim-inspired frontend. Design and phases are in [`docs/tui-plan.md`](docs/tui-plan.md), starting with [Phase 0](#phase-0--tui-backend-prerequisites).
- [ ] **Fully automatic `@search` → `!Bot` flow**
  - **Idea:** a non-interactive mode for scripts and the socket: send `@search`, parse the results (Phase 0.1), pick the best match (prefer `.epub`, a bot that's in the channel, a reasonable size), then request it.
  - **Relation to the TUI:** the TUI is the version where you make that choice yourself.
- [ ] **Extract more than the first file**
  - **Idea:** `extract_ebook()` keeps only the first file from a zip and discards the rest.
- [ ] **Support more DCC variants**
  - **Idea:** add passive/reverse DCC (an offer with port `0`), DCC RESUME, and IPv6 offers. All three are currently rejected or unsupported.

## Known limitations / accepted risks

These were decided on purpose. Revisit them if the threat model changes.

- **The IRC TLS connection isn't verified.** IRCHighway round-robins across 5 or more servers, each with its own self-signed cert, and doesn't publish the list, so pinning would break at random. TLS still protects against passive observers, and each connection logs the server IP and cert fingerprint.
- **DCC transfers are plaintext.** That's how the protocol works. An active attacker on your network could tamper with downloads no matter how the IRC connection is protected.
- **The sender check is "any member of `#ebooks`".** During your search window, any of the roughly 600 channel members can make an offer (to a public IP only, within the size limits).
  - Searches starting with `!Bot` are already strict: only that bot is accepted.
  - Could tighten further to voiced/op users (`+v`/`@`), or a list of known search bots.
- **The download size limit is 100 MiB** (`MAX_DOWNLOAD_BYTES` in `dcc.py`). Raise it if large illustrated PDFs get refused.
- **Downloaded files aren't checked.** A file named `.epub` could contain anything. Open downloads with appropriate care.

## Done

- [x] **Path traversal:** filenames from bots are cleaned up, kept inside `downloads/`, and symlinks are never followed (`O_NOFOLLOW`).
- [x] **Offer filtering:** offers must be sent directly to our nick, arrive during an active search, come from a channel member (or the bot named in `!Bot`), and point at a public IP on port 1024 or above. Stale offers are no longer queued. Also fixed the `:Search` leading-colon bug in the event pattern.
- [x] **No interactive prompt:** the `search>` prompt is gone. `ebooks_agent.py` runs in the foreground, logs requests and results, takes searches only over the Unix socket, and exits cleanly on Ctrl+C or SIGTERM. It no longer needs stdin, so it runs fine under `nohup`, systemd or `< /dev/null`. If the socket can't be opened, startup fails instead of leaving an agent nothing can reach. Tests in `tests/test_main.py`.
- [x] **Configuration:** `config.py` loads the built-in defaults, then `config.toml` (or `--config`, or `$EBOOKS_CONFIG`), then `EBOOKS_<SECTION>_<KEY>` environment variables, then the `--nick` and `--downloads` flags. Unknown keys, wrong types and out-of-range values stop startup with a clear message. `config.example.toml` is the committed, documented template, and `config.toml` is gitignored. Tests in `tests/test_config.py`.
- [x] **No module-import side effects:** `dcc.py` no longer creates `downloads/` on import. `dcc.configure()` creates it at startup.
- [x] **Download reliability:**
  - DCC connects time out after 15s (`CONNECT_TIMEOUT`) and transfers give up after 30s without data (`STALL_TIMEOUT`). There's no limit on total time, so slow but steady downloads finish.
  - The outer 60s catch-all in `EbooksAgent.search` is gone. The 60s offer wait only starts once a search has its turn.
  - A sender that hangs up early raises `IncompleteDownloadError` instead of returning a partial file as a success.
  - Downloads and extractions go to a `name.<random>.part` file (`O_EXCL`), which is hard-linked into place under a free name (`Dune (1).epub`) only when complete. Existing files are never overwritten, and a failure only deletes our own `.part` file.
  - Connection errors become `TransferError`, and corrupt, empty or encrypted zips become `BadArchiveError`. Both are `DownloadError`s.
  - Unexpected errors are logged, the socket handler answers `error: internal error`, and it always closes the connection.
  - Tests in `tests/test_reliability.py`.
- [x] **Size limits:** 100 MiB cap, free-disk check, never reads past the advertised size, zip bomb guards (header check plus counted streaming), and partial files are deleted when a download fails.
- [x] **TLS:** IRC runs over port 6697, encrypt-only (see the accepted risks above).
- [x] **Unix socket:** lives in `$XDG_RUNTIME_DIR` (or a private fallback folder), created as 0600, accepts the same user only, and is cleaned up on exit.
- [x] **Test suite:** `tests/`, run with `pytest`.
- [x] **README:** setup and venv instructions.
