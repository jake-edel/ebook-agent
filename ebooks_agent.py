"""Entry point — connect to IRC and serve searches from the Unix socket.

Runs in the foreground: it stays attached to the terminal and logs what it's
doing (connection, each request, downloads, results) until Ctrl+C or SIGTERM.
Searches come in through the Unix socket; see unix_socket.py.
"""

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from agent import EbooksAgent
from config import Config, ConfigError, load_config
from unix_socket import SocketUnavailableError, unix_socket

log = logging.getLogger(__name__)


async def _serve(agent, stop: asyncio.Event, socket_path: Path | None = None):
    """Serve socket requests until `stop` is set.

    Raises SocketUnavailableError if the socket can't be opened — with no
    prompt, the socket is the only way in, so running without it is pointless.
    """
    server = asyncio.create_task(unix_socket(agent, socket_path))
    stopping = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait({server, stopping}, return_when=asyncio.FIRST_COMPLETED)
        if server in done:
            server.result()  # re-raises a startup failure
            raise SocketUnavailableError("Unix socket server stopped unexpectedly")
    finally:
        for task in (server, stopping):
            task.cancel()
        await asyncio.gather(server, stopping, return_exceptions=True)


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Connect to #ebooks on IRCHighway and download books requested over a Unix socket.",
        epilog="Settings are read from config.toml (see config.example.toml).",
    )
    parser.add_argument("--config", type=Path, metavar="PATH",
                        help="config file to use (default: config.toml next to this script)")
    return parser.parse_args(argv)


async def _main(config: Config):
    # Instantiate our ebooks agent
    agent = EbooksAgent(config)
    # Connect and join the channel before accepting any searches.
    # Ctrl+C here still works: asyncio.run turns it into KeyboardInterrupt.
    await agent.start()

    # From here on, Ctrl+C (SIGINT) and SIGTERM (e.g. `kill`) both mean
    # "shut down cleanly": stop serving, then disconnect from IRC
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    try:
        await _serve(agent, stop)
    finally:
        log.info("Shutting down")
        # A second Ctrl+C during the QUIT wait aborts it, as usual
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.remove_signal_handler(sig)
        await agent.stop()

# __name__ captures the module (entrypoint?) name
# Detects whether this module was run as such
# rather than imported into another. 
if __name__ == "__main__":
    # Python's build in logging library
    # Minimum log level is INFO (DEBUG messages are suppress)
    # Format defines a message template.
    # Set up before loading the config, so config warnings are shown too
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        config = load_config(_parse_args().config)
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)
    try:
        asyncio.run(_main(config))
    except KeyboardInterrupt:
        pass
    except (TimeoutError, SocketUnavailableError) as e:
        # Startup failures — print the reason, not a traceback
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
