# ebooks-agent

A Python agent that connects to IRCHighway's `#ebooks` channel, sends a search term, receives the result via DCC file transfer, and saves the ebook to disk.

## Requirements

- Python 3.11+
- `irc3`

```bash
pip install -r requirements.txt
```

## Usage

### Interactive CLI

```bash
python ebooks_agent.py
```

Starts the agent, connects to IRC, and opens an interactive prompt:

```
search> Dune Frank Herbert
/home/jake/Projects/ebooks-agent/downloads/Dune Frank Herbert.epub
search> exit
```

Type `exit` or `quit` to disconnect, or hit `Ctrl+C`.

### Unix socket

The agent also listens on `/tmp/ebooks.sock` for external input while the CLI is running:

```bash
echo "Foundation Asimov" | socat - UNIX-CONNECT:/tmp/ebooks.sock
```

Returns the file path on success, or an error message on timeout.

## How it works

1. Connects to `irc.irchighway.net:6667` and joins `#ebooks`
2. Sends your search term as a message to the channel
3. Waits for a `DCC SEND` offer from a channel bot (up to 60 seconds)
4. Opens a direct TCP connection to the bot and streams the file to `downloads/`
5. If the file is a `.zip`, extracts the ebook and removes the archive
6. Returns the local file path

## Project structure

```
ebooks_agent.py   — CLI entry point and input loop
agent.py          — EbooksAgent class, owns the IRC connection lifecycle
plugin.py         — irc3 plugin, handles IRC events and DCC offers
unix_socket.py    — Unix socket server for external input
dcc.py            — DCC file transfer and zip extraction
downloads/        — where files are saved
```

## Output

Downloaded files are saved to the `downloads/` directory alongside the script, named after the search term.

## Notes

- The default nick is `fierro_viejo`. Pass a different nick to `EbooksAgent(nick=...)` if needed.
- Connection timeout is 30 seconds, search timeout is 60 seconds.
