"""Entry point for the TUI, while it's being built (see docs/tui-plan.md).

Browse past searches and request books from them. The TUI runs the agent
itself, Unix socket included, so it can't run beside ebooks_agent.py.
"""

import argparse
import logging
import sys
from pathlib import Path

from textual.theme import BUILTIN_THEMES

from server.agent import EbooksAgent
from config import ConfigError, config_file, load_config
from server.logfile import LogFile, LogFileError
from tui.app import EbooksApp
from server.unix_socket import SocketUnavailableError, claim_socket_path


def main(argv=None):
    parser = argparse.ArgumentParser(description="Search #ebooks and request books, in the terminal.")
    parser.add_argument("--config", type=Path, metavar="PATH",
                        help="config file to use (default: config.toml next to this script)")
    args = parser.parse_args(argv)

    # The TUI owns the terminal: log lines printed to it would garble the
    # screen. They go to the log file only; until that's open, they're
    # dropped (a handler on the root logger also stops Python printing
    # warnings as a last resort).
    logging.getLogger().addHandler(logging.NullHandler())

    try:
        config = load_config(args.config)
        check_theme(config.tui.theme)
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)
    try:
        # Before connecting: another agent may already be running
        socket_path = claim_socket_path()
    except SocketUnavailableError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    try:
        # After claiming the socket: only one process may write the log
        log_file = LogFile(config.log, "ebooks_tui", config_file(args.config))
    except LogFileError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    with log_file:
        EbooksApp(EbooksAgent(config), config.downloads.searches_dir, socket_path,
                  theme=config.tui.theme).run()


def check_theme(name: str):
    """Raise ConfigError unless `name` is one of Textual's themes. Here, not in
    config.py, which the headless agent loads without Textual."""
    if name not in BUILTIN_THEMES:
        raise ConfigError(f"tui.theme: unknown theme {name!r} "
                          f"(one of: {', '.join(sorted(BUILTIN_THEMES))})")


if __name__ == "__main__":
    main()
