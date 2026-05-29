# ebooks-agent

A standalone Python agent that connects to IRCHighway's `#ebooks` channel, submits a search command, receives the results via DCC file transfer, and saves the `.zip` to disk.

## Requirements

- Python 3.8+
- `irc3`

```bash
pip install -r requirements.txt
```

## Usage

### Command line

```bash
python ebooks_agent.py "Dune Herbert"
```

Prints the path to the downloaded `.zip` file on success.

### As a library

```python
import asyncio
from ebooks_agent import EbooksAgent

async def main():
    agent = EbooksAgent(nick="fierro_viejo")
    zip_path = await agent.search("Foundation Asimov")
    print(zip_path)  # e.g. downloads/Search_Results_Foundation_Asimov.zip

asyncio.run(main())
```

## How it works

1. Connects to `irc.irchighway.net:6667` and joins `#ebooks`
2. Sends `@search <term>` to the channel
3. Waits for a `DCC SEND` offer from the search bot (up to 60 seconds)
4. Opens a direct TCP connection to the bot and streams the `.zip` file to `downloads/`
5. Returns the local file path

## Output

Downloaded files are saved to the `downloads/` directory alongside the script.

## Notes

- The agent waits 5 seconds after joining before submitting the search, to give the channel bot time to register the new connection. If searches consistently time out, this delay may need to be increased.
- The default nick is `fierro_viejo`. Pass a different nick to `EbooksAgent(nick=...)` if needed.
- The search timeout defaults to 60 seconds and can be overridden: `agent.search(term, timeout=120)`.
