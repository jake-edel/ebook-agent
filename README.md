# ebooks-agent

A Python agent that connects to IRCHighway's `#ebooks` channel, sends a search term, receives the result via DCC file transfer, and saves the ebook to disk.
## Requirements

- Python 3.11+
- `irc3`


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

`config.toml` is gitignored, so your local settings stay out of commits. `config.example.toml` lists every setting with its default and what it does: IRC host, nick and channel, the timeouts, the size limits, and the downloads folder.

Settings are applied in this order, each overriding the one before:

1. Built-in defaults
2. The config file. That's `config.toml` next to `ebooks_agent.py`, or the file you pass with `--config PATH`, or the one named in `$EBOOKS_CONFIG`
3. Environment variables named `EBOOKS_<SECTION>_<KEY>`, for example `EBOOKS_IRC_NICK=my_nick` or `EBOOKS_DOWNLOADS_DIR=~/Books`
4. Command-line flags: `--nick` and `--downloads`

```bash
python ebooks_agent.py --nick my_nick --downloads ~/Books
EBOOKS_SEARCH_OFFER_TIMEOUT=120 python ebooks_agent.py
```

A relative `downloads.dir` in the config file is relative to that file. From an environment variable or flag, it's relative to the current directory.

Mistakes are caught at startup with a clear message, and the agent exits without connecting. That covers unknown settings (typos like `nik`), wrong types, and out-of-range values. TLS can't be turned off.

## Usage

Start the agent:

```bash
python ebooks_agent.py
```

It connects to IRC and then runs in the foreground, logging what it does: connecting, each request, downloads, and where each file was saved. There's no prompt. Searches come in over a Unix socket instead:

```
INFO Connected to irc.irchighway.net and joined #ebooks
INFO Listening on /run/user/1000/ebooks.sock
INFO Send a search with: echo 'Dune Frank Herbert' | socat - UNIX-CONNECT:/run/user/1000/ebooks.sock
INFO Ready. Press Ctrl+C to stop.
INFO Request: Dune Frank Herbert
...
INFO Saved: /home/jake/Projects/ebooks-agent/downloads/Dune Frank Herbert.epub
```

Press `Ctrl+C`, or send `SIGTERM`, to disconnect and exit.

### Sending searches

From another terminal, or from a script, write one search per connection to the socket:

```bash
echo "Foundation Asimov" | socat - UNIX-CONNECT:$XDG_RUNTIME_DIR/ebooks.sock
```

The socket path is logged at startup. It's normally `$XDG_RUNTIME_DIR/ebooks.sock`, i.e. `/run/user/<your uid>/ebooks.sock`.

Only your user can use it: the socket is mode `0600` inside a directory only you can access, and connections from other users are rejected. If `XDG_RUNTIME_DIR` isn't set (e.g. some cron or SSH sessions), it falls back to a private `/tmp/ebooks-<uid>/` directory. If the socket can't be created, the agent exits with an error, since there would be no way to send it searches.

The reply is the saved file's path, or a line starting with `error:` explaining what went wrong.

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

The tests never touch the network or your real `downloads/` folder — each test gets its own temporary downloads directory, and DCC senders are faked locally.

## How it works

1. Connects to `irc.irchighway.net:6697` over TLS and joins `#ebooks`
2. Waits for search terms on the Unix socket and sends each one as a message to the channel
3. Waits for a `DCC SEND` offer from a channel bot (up to 60 seconds)
4. Opens a direct TCP connection to the bot and streams the file into a temporary `.part` file in `downloads/`
5. Once every byte has arrived, gives the file its real name. If that name is taken, it uses `Dune (1).epub` and so on rather than overwriting anything
6. If the file is a `.zip`, extracts the ebook the same way and removes the archive
7. Returns the local file path

## Project structure

```
ebooks_agent.py   — entry point: flags, startup, runs until Ctrl+C
config.py         — loads and validates settings (defaults, config.toml, env, flags)
config.example.toml — documented template for config.toml
agent.py          — EbooksAgent class, owns the IRC connection lifecycle
plugin.py         — irc3 plugin, handles IRC events and DCC offers
unix_socket.py    — Unix socket server, the agent's input
dcc.py            — DCC file transfer and zip extraction
results.py        — parser for SearchBot result files
downloads/        — where files are saved
tests/            — pytest suite (paths, offers, plugin gating, size limits, socket)
TODO.md           — outstanding work and accepted risks
docs/tui-plan.md  — design and phases for the planned TUI
```

## Output

Downloaded files are saved to the `downloads/` directory alongside the script (or `downloads.dir` from the config), named after the search term.

## Notes

- The default nick is `pt_bookbot`. Change it with `--nick`, `EBOOKS_IRC_NICK`, or `nick` in `config.toml`.
- Timeouts (all configurable):
  - 30 seconds to connect to IRC.
  - 60 seconds for a bot to offer the file.
  - 15 seconds to connect to the bot for the transfer.
  - A transfer gives up after 30 seconds without data. There's no limit on total download time.
- A failed or interrupted download never leaves a partial file behind, and never touches an existing file with the same name.
- **TLS is encrypt-only.** IRCHighway rotates DNS across several servers, each with its own self-signed certificate, so certificates aren't verified. This keeps your searches private from passive observers (shared Wi-Fi, your ISP) but not from an active man-in-the-middle. Each connection logs the server IP and certificate fingerprint.
- **DCC file transfers are plaintext** — that's how the DCC protocol works, and it's independent of the IRC connection's TLS.
