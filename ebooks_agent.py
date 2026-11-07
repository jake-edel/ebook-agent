"""CLI entry point — start the agent and accept search terms interactively."""

import argparse
import asyncio
import logging
import sys
from pathlib import Path

from agent import EbooksAgent
from config import Config, ConfigError, load_config
from dcc import DownloadError
from unix_socket import unix_socket

log = logging.getLogger(__name__)


async def _input_loop(agent: EbooksAgent):
    # Continuously listen for inputs from an interactive shell
    while True:
        try:
            # Create a new thread for the blocking input function
            # Grab the search term from the shell
            term = (await asyncio.to_thread(input, "search> ")).strip()
        except EOFError:
            break
        if not term:
            continue
        if term.lower() in ("exit", "quit"):
            break
        try:
            # Once we get a search term
            # Pass it to the agent
            # and await a response
            path = await agent.search(term)
            print(path)
        except (TimeoutError, DownloadError) as e:
            print(f"Error: {e}")
        except Exception:
            # Anything unexpected is a bug, but one failed search shouldn't
            # end the whole session — log the traceback and keep going
            log.exception("Search for %r failed unexpectedly", term)


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Search #ebooks on IRCHighway and download the results.",
        epilog="Settings are read from config.toml (see config.example.toml), "
               "then EBOOKS_* environment variables, then these flags.",
    )
    parser.add_argument("--config", type=Path, metavar="PATH",
                        help="config file to use (default: config.toml next to this script, "
                             "or $EBOOKS_CONFIG)")
    parser.add_argument("--nick", help="IRC nick to connect as")
    parser.add_argument("--downloads", metavar="DIR", help="directory to save files in")
    return parser.parse_args(argv)


def _load(args: argparse.Namespace) -> Config:
    return load_config(
        path=args.config,
        overrides={("irc", "nick"): args.nick, ("downloads", "dir"): args.downloads},
    )


async def _main(config: Config):
    # Instantiate our ebooks agent
    agent = EbooksAgent(config)
    # Wait for the agent to start and connect to the server
    # We don't want to handle any input until it's ready to receive
    await agent.start()

    # Spawn a new task to handle the unix socket
    # Pass it a reference to the agent so we can feed
    # it input from the socket
    socket_task = asyncio.create_task(unix_socket(agent))
    try:
        # Start our input loop
        # The unix socket will run on a different task
        # while this loop continually listens for input
        # from the CLI
        await _input_loop(agent)
    finally:
        # When we exit out, we close our socket and end the task
        # and disconnect agent from IRC server
        socket_task.cancel()
        agent.stop()

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
        config = _load(_parse_args())
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)
    try:
        asyncio.run(_main(config))
    except KeyboardInterrupt:
        pass
    except TimeoutError as e:
        # Startup failures — print the reason, not a traceback
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
