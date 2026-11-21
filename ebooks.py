"""Entry point: `ebooks` runs the TUI, `ebooks --headless` the bare agent.

Both connect to IRC and serve searches from the Unix socket (see
server/unix_socket.py); only one of them can run at a time.

- The TUI (the default) browses past searches and requests books from them,
  with the agent running in its process.
- `--headless` runs in the foreground with no interface: it stays attached to
  the terminal and logs what it's doing (connection, each request, downloads,
  results) until Ctrl+C or SIGTERM. Searches come in through the socket only.
- `--socket-path` prints where the socket is, for clients, and exits.

Textual is imported on the TUI path only, so the other two never load it.
"""

import argparse
import asyncio
import logging
import signal
import sys
from pathlib import Path

from server.agent import EbooksAgent
from config import Config, ConfigError, config_file, load_config
from server.logfile import LogFile, LogFileError
from server.unix_socket import (
    SocketUnavailableError,
    UnsafeSocketPathError,
    claim_socket_path,
    socket_path,
    unix_socket,
)

log = logging.getLogger(__name__)


def _parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ebooks",
        description="Search #ebooks on IRCHighway and request books, in the terminal.",
        epilog="Settings are read from config.toml (see config.example.toml).",
    )
    parser.add_argument("--config", type=Path, metavar="PATH",
                        help="config file to use (default: config.toml next to this script)")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--headless", action="store_true",
                      help="no TUI: log to the terminal and take requests over the "
                           "Unix socket only, until Ctrl+C")
    mode.add_argument("--socket-path", action="store_true",
                      help="print the Unix socket's path and exit (for clients, "
                           "so they don't have to work it out themselves)")
    return parser.parse_args(argv)


def main(argv=None):
    args = _parse_args(argv)
    if args.socket_path:
        _print_socket_path()
    elif args.headless:
        _headless(args.config)
    else:
        _tui(args.config)


def _print_socket_path():
    # Logs go to stderr (a warning about XDG_RUNTIME_DIR, say), which keeps
    # stdout clean for the path
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        print(socket_path())
    except UnsafeSocketPathError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


# --- The TUI ---------------------------------------------------------------

def _tui(config_path: Path | None):
    # The TUI owns the terminal: log lines printed to it would garble the
    # screen. They go to the log file only; until that's open, they're
    # dropped (a handler on the root logger also stops Python printing
    # warnings as a last resort).
    logging.getLogger().addHandler(logging.NullHandler())

    try:
        config = load_config(config_path)
        check_theme(config.tui.theme)
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)
    try:
        # Before connecting: another agent may already be running
        path = claim_socket_path()
    except SocketUnavailableError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    try:
        # After claiming the socket: only one process may write the log
        log_file = LogFile(config.log, "ebooks", config_file(config_path))
    except LogFileError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    from tui.app import EbooksApp

    with log_file:
        EbooksApp(EbooksAgent(config), config.downloads.searches_dir, path,
                  theme=config.tui.theme).run()


def check_theme(name: str):
    """Raise ConfigError unless `name` is one of Textual's themes. Here, not in
    config.py, which headless mode loads without Textual."""
    from textual.theme import BUILTIN_THEMES

    if name not in BUILTIN_THEMES:
        raise ConfigError(f"tui.theme: unknown theme {name!r} "
                          f"(one of: {', '.join(sorted(BUILTIN_THEMES))})")


# --- Headless --------------------------------------------------------------

def _headless(config_path: Path | None):
    # Set up before loading the config, so config warnings are shown too.
    # Logs go to stderr.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    try:
        config = load_config(config_path)
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)
    try:
        asyncio.run(_main(config, config_file(config_path)))
    except KeyboardInterrupt:
        pass
    except (TimeoutError, SocketUnavailableError, LogFileError) as e:
        # Startup failures — print the reason, not a traceback
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


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


async def _main(config: Config, config_path: Path | None = None):
    # Before going near IRC: if another agent is running, connecting a second
    # one (as "ebooks_cli_") would only be undone a moment later. Also before
    # the log file, which only one process may write.
    path = claim_socket_path()
    with LogFile(config.log, "ebooks --headless", config_path):
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


if __name__ == "__main__":
    main()
