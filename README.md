# ebooks-agent

A Python agent for IRCHighway's `#ebooks` channel. It sends `@search` queries and saves the results file, then requests the books you pick from it (`!Bot …` lines), receiving each over DCC and saving it to disk. Other programs drive it over a Unix socket.

## Requirements

- Python 3.11+
- `irc3`, and `textual` for the TUI

## Setup

First time only — create a virtual environment in `.venv/` and install dependencies into it:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Every new terminal session — activate the venv before running the agent:

```bash
source .venv/bin/activate
```

Your prompt should now start with `(.venv)`. To confirm, `which python` should point inside the project's `.venv/`. Run `deactivate` to leave the venv.

Using fish or csh? Use `source .venv/bin/activate.fish` or `source .venv/bin/activate.csh` instead.

Skip activation entirely by calling the venv's Python directly:

```bash
.venv/bin/python ebooks_agent.py
```

After pulling changes that touch `requirements.txt`, re-run `pip install -r requirements.txt` with the venv active.

## Configuration

Everything works out of the box with the defaults. To change something, copy the template and edit it:

```bash
cp config.example.toml config.toml
```

`config.toml` is gitignored, so your local settings stay out of commits. `config.example.toml` lists every setting with its default and what it does: IRC host, nick and channel, the timeouts, the size limits, the downloads folder, a command to run on each finished book (e.g. to move it into a library), and the log file.

Any setting the file leaves out keeps its built-in default. The agent reads `config.toml` next to `ebooks_agent.py`, or another file if you pass one:

```bash
python ebooks_agent.py --config ~/ebooks.toml
```

Relative paths in the config file (`downloads.dir`, `log.file`, ...) are relative to that file.

Mistakes are caught at startup with a clear message, and the agent exits without connecting. That covers unknown settings (typos like `nik`), wrong types, and out-of-range values. TLS can't be turned off.

## Usage

Start the agent:

```bash
python ebooks_agent.py
```

It connects to IRC and then runs in the foreground, logging what it does: connecting, each request, downloads, and where each file was saved. There's no prompt. Commands come in over a Unix socket instead:

```
INFO Connected to irc.irchighway.net, joining #ebooks
INFO Joined #ebooks
INFO Listening on /run/user/1000/ebooks.sock
INFO Ready. Press Ctrl+C to stop.
INFO Request: @search dune frank herbert
...
INFO Saved: /home/jake/ebooks-cli/searches/20261108-143005 dune frank herbert.txt
```

Press `Ctrl+C`, or send `SIGTERM`, to disconnect and exit.

### The log file

Both the agent and the TUI also log to `agent.log` (set by `log.file` in the config), so there's a record to look back on. The file is appended to across runs: each run starts with a `---- session start` line and ends with `---- session end`. A run with no end line crashed or was killed. Every line has a timestamp:

```
2026-11-11 14:02:10.950 INFO  ---- session start: ebooks_agent pid 48211, config /home/jake/ebooks-cli/config.toml, UTC+0100
2026-11-11 14:03:01.010 INFO  Message sent: @search dune frank herbert
...
2026-11-11 14:30:00.010 INFO  ---- session end
```

At 5 MiB the file is rotated: the old one becomes `agent.log.1`, and the 3 most recent old files are kept. The size and count can be changed in the config. The files are readable by you only, since they hold your searches and the books you asked for. See [`docs/logging-plan.md`](docs/logging-plan.md) for where this is going.

### The TUI (in progress)

```bash
python ebooks_tui.py
```

It connects to IRC and browses your past searches in `searches/`. Move with `j`/`k` (or the arrows), `Enter`, `l` or `→` to go into a search's results, and `h`, `←` or `Esc` to come back. Results list the epubs first, then the other formats below a divider. `Enter` on a result requests that book: the first column shows how it's going (`…` waiting, `42%`, `⇢` being moved by the post-download hook, `✓` done, `⚠` the hook failed, `✗` failed), and the status bar has the details.

`s` opens a search prompt (Enter sends it, Esc cancels). The search shows up at the top of the sidebar at once, and its results open when they arrive. `d` on a past search deletes its file, after a `y/n`.

Every book requested this session, from the TUI or over the socket, is listed under BOOKS at the top of the sidebar, newest first: `…` waiting, `⇣` arriving, `⇢` being moved by the hook, `✓` done, `⚠` the hook failed (the book is still in `downloads/`), `✗` failed. Highlighting one shows its details in the main pane, with the hook's outcome and what the bot said. `d` dismisses a finished one from the list (nothing on disk is touched). The list isn't kept between sessions.

`Tab` switches to the log, full screen, and back: everything that goes to `agent.log`, as it happens (what was sent, what bots said to you, DCC offers including refused ones, the hook's output). `j`/`k`, `gg`/`G`; it follows new lines while you're at the bottom. It only has this session: earlier ones are in `agent.log`.

`q` quits, and asks first if a request is still in progress or a book is still being moved.

The colours come from a theme: `[tui] theme` in `config.toml`. The default, `ansi-dark`, uses your terminal's own colours, so the TUI follows your terminal's theme; `config.example.toml` lists the others (`tokyo-night`, `gruvbox`, `catppuccin-mocha`...). The widgets are styled in `tui/app.tcss`, and the colours of text inside them by meaning (failed, warning, muted...) in `tui/styles.py`. Quitting waits for a running hook to finish, rather than killing it partway.

The TUI serves the Unix socket too, so socket clients keep working while it's open (their requests show up in the TUI). It can't run beside `ebooks_agent.py`. What's next: see [`docs/tui-plan.md`](docs/tui-plan.md).

### The socket

Ask the agent where its socket is rather than working it out yourself:

```bash
python ebooks_agent.py --socket-path
# -> /run/user/1000/ebooks.sock
```

It's normally `$XDG_RUNTIME_DIR/ebooks.sock`. If `XDG_RUNTIME_DIR` isn't set or isn't private (e.g. some cron or SSH sessions), it falls back to a private `/tmp/ebooks-<uid>/` directory.

Only your user can use it: the socket is mode `0600` inside a directory only you can access, and connections from other users are rejected. If the socket can't be created, the agent exits with an error, since nothing could reach it. The same goes for starting a second agent while one is running: it exits before connecting to IRC, and leaves the first one's socket alone.

### Sending commands

The protocol is JSON lines. Connect, send one command, and read replies until the agent closes the connection:

```bash
SOCK=$(python ebooks_agent.py --socket-path)

# Search: the results file is saved in searches/
echo '{"cmd": "search", "query": "Foundation Asimov"}' | socat -t 600 - UNIX-CONNECT:$SOCK

# Request a book: a "!Bot ..." line from a results file (the part before " ::INFO::")
echo '{"cmd": "request", "line": "!Bsk Isaac Asimov - Foundation.epub"}' | socat -t 600 - UNIX-CONNECT:$SOCK

# Connection status
echo '{"cmd": "status"}' | socat - UNIX-CONNECT:$SOCK
```

(`-t 600` keeps socat listening after it has sent the command. By default it gives up half a second later, long before a reply arrives.)

The replies to `search` and `request` are the agent's own events, one JSON object per line, for that request only:

```json
{"type": "RequestUpdated", "request": {"id": "1", "kind": "search", "line": "@search Foundation Asimov", "state": "pending", ...}}
{"type": "RequestUpdated", "request": {"id": "1", "state": "waiting", ...}}
{"type": "BotMessage", "nick": "Search", "text": "...", "request_id": "1"}
{"type": "RequestUpdated", "request": {"id": "1", "state": "transferring", "filename": "...", "size": 51234, "received": 0, ...}}
{"type": "RequestUpdated", "request": {"id": "1", "state": "done", "path": "/home/jake/ebooks-cli/searches/20261108-143005 Foundation Asimov.txt", ...}}
```

The last line is always a `RequestUpdated` whose `state` is `done` (see `path`), `failed` (see `error`) or `cancelled`. For a book, with a post-download hook configured (`downloads.on_complete`), the `done` snapshot has `"hook": "running"`, and the replies go on until the hook finishes: the last one has `"hook": "done"`, or `"failed"` with the reason in `hook_error` (the book is then still at `path`). `hook` is `null` for searches, and when there's no hook. So a client should stop at the first snapshot that's finished and whose `hook` isn't `running`, or simply read until the connection closes. A command that can't be carried out gets `{"type": "Error", "error": "..."}` instead, and so does a client still waiting when the agent shuts down. If you hang up early, the request carries on and the file is still saved.

Limits: the command must arrive within 10 seconds of connecting and be at most 8 KiB, and the line sent to IRC (`@search <query>` or the `!Bot` line) at most 350 bytes.

One example client is a Neovim `:Ebook` command (`~/.dotfiles/nvim/lua/config/ebook.lua`), maintained outside this repo.

## Testing

Install the dev dependencies once (with the venv active):

```bash
pip install -r requirements-dev.txt
```

Then run the suite from the project root:

```bash
pytest            # or: .venv/bin/python -m pytest
pytest -v         # list every test
pytest tests/test_limits.py   # just one file
```

The tests never touch the network or your real `downloads/` and `searches/` folders. Each test gets its own temporary directories, and DCC senders are faked locally.

## How it works

1. Connects to `irc.irchighway.net:6697` over TLS and joins `#ebooks`
2. Waits for commands on the Unix socket and sends each one to the channel, as `@search <query>` or as the `!Bot …` request line
3. Waits for a `DCC SEND` offer from a channel bot (up to 60 seconds). For a `!Bot` request, only that bot may answer
4. Opens a direct TCP connection to the bot and streams the file into a temporary `.part` file, in `searches/` for results and `downloads/` for books
5. Once every byte has arrived, gives the file its real name. If that name is taken, it uses `Dune (1).epub` and so on rather than overwriting anything
6. If the file is a `.zip`, extracts the ebook the same way and removes the archive. If the zip holds other files too, only the best ebook is extracted and the zip is kept, so nothing is lost. A zip that can't be extracted is deleted and the request fails
7. Reports the local file path

## Project structure

```
ebooks_agent.py   — entry point: flags, startup, runs until Ctrl+C
ebooks_tui.py     — entry point for the TUI (in progress)

core.py           — the core interface: requests, events and status, shared by every frontend
config.py         — loads and validates settings (defaults, overridden by config.toml)
config.example.toml — documented template for config.toml
results.py        — parser for SearchBot result files

server/           — the agent side
  agent.py        — EbooksAgent class, owns the IRC connection and implements the core interface
  plugin.py       — irc3 plugin, handles IRC events and DCC offers
  dcc.py          — DCC file transfer and zip extraction
  hook.py         — the post-download hook (downloads.on_complete)
  unix_socket.py  — Unix socket server (JSON lines), the agent's input
  logfile.py      — the log file: format, session lines, rotation

tui/              — the TUI: app.py (interface and keys), library.py (reads searches from disk)

downloads/        — where books are saved
searches/         — where search results files are saved
tests/            — pytest suite
TODO.md           — outstanding work and accepted risks
docs/core-interface.md — what the agent offers frontends, and why
docs/tui-plan.md  — design and phases for the planned TUI
docs/logging-plan.md — what the log file records, and the stages to get there
```

## Output

- **Search results** go to `searches/` (or `downloads.searches_dir`), named `<timestamp> <query>.txt`, for example `20261108-143005 dune.txt`. Older searches are kept, and a repeat gets a new file.
- **Books** go to `downloads/` (or `downloads.dir`), keeping the name the bot sent (sanitized). For a zipped book, that's the name of the file inside the zip.

Both folders sit alongside the script by default.

## Notes

- Your nick, and the username and real name a `/whois` on you shows, are `nick`, `username` and `realname` in `config.toml` (defaults in `config.example.toml`). Left to irc3, the last two would announce you as an irc3 bot.
- Timeouts (all configurable):
  - 30 seconds to connect to IRC.
  - 60 seconds for a bot to offer the file.
  - 15 seconds to connect to the bot for the transfer.
  - A transfer gives up after 30 seconds without data. There's no limit on total download time.
- **Staying polite on the server:**
  - A bot gets at most 2 book requests from you at a time (`downloads.max_requests_per_bot`), from sending the line until its file has arrived. More wait their turn, unsent.
  - Searches go one at a time.
  - After a dropped or refused connection, retries wait longer each time: about 3 seconds, then 10, 30, 60 and 120, then every 5 minutes, until you're back in the channel.
- A failed or interrupted download never leaves a partial file behind, and never touches an existing file with the same name.
- **TLS is encrypt-only.** IRCHighway rotates DNS across several servers, each with its own self-signed certificate, so certificates aren't verified. This keeps your searches private from passive observers (shared Wi-Fi, your ISP) but not from an active man-in-the-middle. Each connection logs the server IP and certificate fingerprint.
- **DCC file transfers are plaintext** — that's how the DCC protocol works, and it's independent of the IRC connection's TLS.
