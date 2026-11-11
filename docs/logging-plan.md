# Logging plan

A log file you can audit: what the agent did, what it sent to IRC, what was sent to it, and how each request went. It works the same whether the agent runs headless (`ebooks_agent.py`) or inside the TUI.

## Background

Today, modules log with the standard `logging` library:

- `ebooks_agent.py` sends it all to stderr as `%(levelname)s %(message)s`, with no timestamps.
- The TUI drops it all (`ebooks_tui.py`), because log lines would garble the screen. A TUI session leaves no record at all.

What's logged is a decent start: connect, join and disconnect; the TLS peer and fingerprint; every line we send; messages sent to us (if `irc.show_private_messages` is on); DCC offers, including rejected and unsolicited ones; finished downloads and extractions; and socket activity.

What's missing for auditing:

- **Timestamps.**
- **Request ids.** You can't tell which `Message sent`, bot reply and DCC offer belong together.
- **Request state changes.** Pending, waiting, transferring, done, failed and cancelled are only sent as events (`RequestUpdated`), never logged. So a cancellation, or a failure with its reason (e.g. the offer timeout), leaves no trace. The only exception is unexpected errors, which `agent.py` logs with a traceback.

## Decisions

- **Plain text, one line per entry.** It's meant to be read with `less` and searched with `grep`, not parsed by scripts.
- **Only IRC traffic that concerns us:** lines we send, and everything addressed to us directly (DMs, notices, CTCP/DCC, server notices). No channel chatter, and no raw protocol lines.
- **The file always records messages sent to us.** `irc.show_private_messages` only controls whether they're also printed to the console.
- **One file, appended to across runs.** Each run starts with a session line. The file rotates by size: **5 MiB per file, 3 old files kept** (`agent.log.1` … `.3`), so at most 20 MiB on disk. At the current rate of a few hundred bytes per request, 5 MiB covers months of use.
- **Always on.** There's no level setting or off switch until there's a reason for one.

## What the file looks like

```
2026-11-11 14:02:10.950 INFO  ---- session start: ebooks_agent pid 48211, config /home/me/ebook-agent/config.toml
2026-11-11 14:02:11.384 INFO  conn   connecting to irc.irchighway.net:6697 as anjay
2026-11-11 14:02:12.901 INFO  conn   TLSv1.3 to 192.0.2.10 (cert sha256 3f9a1c0d…, unverified)
2026-11-11 14:02:14.220 INFO  conn   ready in #ebooks
2026-11-11 14:03:01.005 INFO  req#1  pending: @search dune herbert
2026-11-11 14:03:01.010 INFO  irc    >> #ebooks: @search dune herbert                       req#1
2026-11-11 14:03:01.011 INFO  req#1  waiting
2026-11-11 14:03:02.540 INFO  irc    << NOTICE Search: Your search for "dune herbert" has been accepted…  req#1
2026-11-11 14:03:05.112 INFO  irc    << DCC SEND Search: results.txt.zip from 198.51.100.7:4000 (18234 bytes)  req#1
2026-11-11 14:03:05.113 INFO  req#1  transferring results.txt.zip (18234 bytes)
2026-11-11 14:03:05.870 INFO  req#1  done: /home/me/ebook-agent/searches/20261111-140305 dune herbert.txt (4.9s)
2026-11-11 14:04:40.000 WARN  irc    << DCC SEND Foo ignored: not in #ebooks
2026-11-11 14:05:02.118 INFO  req#2  pending: !Bsk Frank Herbert - Dune.epub
2026-11-11 14:07:12.300 WARN  req#2  failed: No DCC offer received within 60s for '!Bsk Frank Herbert - Dune.epub'
2026-11-11 14:09:00.000 INFO  req#3  cancelled (was waiting)
2026-11-11 14:30:00.000 INFO  conn   disconnected from irc.irchighway.net
2026-11-11 14:30:00.010 INFO  ---- session end
```

- **Timestamp:** local time with milliseconds. Each session line also records the UTC offset, so entries can be compared across DST changes.
- **Level:** `INFO`, `WARN` or `ERROR`. Tracebacks follow their `ERROR` line, indented.
- **Tag:**
  - `conn`: connection and TLS.
  - `irc`: `>>` for a line we sent, `<<` for one sent to us.
  - `req#N`: a request's state changes.
  - `sock`: Unix socket activity.
  - `----`: session start and end.
- **Request ids:** `irc` lines get a trailing `req#N` when they belong to a request, linked the way offers are matched (`EbooksPlugin._match`). So `grep 'req#2'` shows a request's whole history.
- **Progress updates during a transfer aren't logged.** Only state changes are.
- **Request ids start again at 1 in every session**, which is why session lines matter: the same `req#1` appears once per run.

## Design

### Where it goes

`agent.log` in the project directory by default, next to `downloads/` and `searches/`. It can be changed in the config (TOML only, like every other setting):

```toml
[log]
# Relative paths are relative to this file; ~ works
file = "agent.log"
# Rotate when the file reaches this size; this many old files are kept
max_size_mib = 5
backups = 3
```

The file is created with mode 0600: it holds your searches, the books you asked for and bots' IP addresses. `*.log*` goes in `.gitignore`.

### How it's written

A new module, `logfile.py`, sets up a `RotatingFileHandler` on the root logger, with the formatter above. Both entry points call it once the config is loaded **and the socket path is claimed**. A second instance that fails to claim the socket exits without touching the file, so only one process ever writes it or rotates it.

- **`ebooks_agent.py`:** the console handler stays as it is. A filter on it hides messages sent to us when `show_private_messages` is off.
- **`ebooks_tui.py`:** the file handler replaces the `NullHandler`, so TUI sessions get logged too. Nothing goes to the console.

The message-logging lines get their own logger name (`plugin.messages`) so the console filter can find them without parsing text.

### What's added to the log

Each change is logged where it happens, not by listening to the event stream. A listener would run later than the code around it, so its lines could appear out of order with the `>>`/`<<` lines, and the last few could be lost at shutdown.

- **`EbooksAgent._set`:** one line each time a request's `state` changes (the progress-only updates are skipped). It includes the line, filename/size, path, error, or the state it was cancelled from.
- **`EbooksAgent._set_state` / `start()` / `stop()`:** connection changes, worded as above.
- **`EbooksPlugin._fetch` / `on_message` / `on_dcc_send`:** add the request key to the `>>` and `<<` lines, and log accepted DCC offers as `<<` lines.
- **Session start/end:** `logfile.py` logs the start line, and an `atexit` hook logs the end line. A missing end line means the process crashed or was killed.

## Stages

1. **The file.** ✅ Done. `[log]` config, `logfile.py`, rotation, the 0600 mode, session lines, timestamps, and wiring into both entry points (so the TUI logs too). The existing log calls go to the file unchanged.
   - `backups` must be at least 1: `RotatingFileHandler` never rotates with 0, so the file would grow without limit.
   - The session end line records what ended a run, if it wasn't a normal shutdown (e.g. `---- session end: TimeoutError: Could not connect…`).
   - irc3 ran its own `logging.config.dictConfig`, which gave its loggers a stderr handler that doesn't propagate. That kept its errors out of the file, printed them over the TUI, and closed every handler already set up. `_IrcBot.logging_config` is now an empty incremental config, so irc3 leaves logging alone.
2. **The audit trail.** Request state lines, `req#N` on IRC lines, the `conn`/`irc`/`sock` tags, `>>`/`<<` wording, and the `show_private_messages` console filter. Update the tests that check log output (`tests/test_plugin.py`).
3. **Later: a log view in the TUI.** It reads the live log records through a handler, not the file, and adds a buffer that can be filtered by request. It belongs to the TUI plan.
