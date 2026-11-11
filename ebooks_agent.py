"""Entry point — connect to IRC and serve searches from the Unix socket.

Runs in the foreground: it stays attached to the terminal and logs what it's
doing (connection, each request, downloads, results) until Ctrl+C or SIGTERM.
Searches come in through the Unix socket; see unix_socket.py.
`--socket-path` prints where that is, for clients, and exits.
"""

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from agent import EbooksAgent
from config import Config, ConfigError, config_file, load_config
from logfile import LogFile, LogFileError
from unix_socket import (
    SocketUnavailableError,
    UnsafeSocketPathError,
    claim_socket_path,
    socket_path,
    unix_socket,
)

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
    parser.add_argument("--socket-path", action="store_true",
                        help="print the Unix socket's path and exit (for clients, "
                             "so they don't have to work it out themselves)")
    return parser.parse_args(argv)


async def _main(config: Config, config_path: Path | None = None):
    # Before going near IRC: if another agent is running, connecting a second
    # one (as "ebooks_cli_") would only be undone a moment later. Also before
    # the log file, which only one process may write.
    path = claim_socket_path()
    with LogFile(config.log, "ebooks_agent", config_path):
        await _run(config, path)


async def _run(config: Config, path: Path):
    agent = EbooksAgent(config)
    await agent.start()
    loop = asyncio.get_running_loop()
    try:
        # Join the channel before accepting any commands, so a bad host or
        # nick fails at startup. Ctrl+C here still works: asyncio.run turns
        # it into KeyboardInterrupt.
        await agent.wait_ready(config.irc.connect_timeout)

        # From here on, Ctrl+C (SIGINT) and SIGTERM (e.g. `kill`) both mean
        # "shut down cleanly": stop serving, then disconnect from IRC
        stop = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop.set)
        try:
            await _serve(agent, stop, path)
        finally:
            log.info("Shutting down")
            # A second Ctrl+C during the QUIT wait aborts it, as usual
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.remove_signal_handler(sig)
    finally:
        # Also after a failed or interrupted start, so irc3 stops retrying
        await agent.stop()


def main(argv=None):
    # Set up before loading the config, so config warnings are shown too.
    # Logs go to stderr, which keeps --socket-path's stdout clean.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = _parse_args(argv)
    if args.socket_path:
        try:
            print(socket_path())
        except UnsafeSocketPathError as e:
            print(f"Error: {e}", file=sys.stderr)
            sys.exit(1)
        return
    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)
    try:
        asyncio.run(_main(config, config_file(args.config)))
    except KeyboardInterrupt:
        pass
    except (TimeoutError, SocketUnavailableError, LogFileError) as e:
        # Startup failures — print the reason, not a traceback
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
