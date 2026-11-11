"""Entry point for the TUI, while it's being built (see docs/tui-plan.md).

Browse past searches and request books from them. The TUI runs the agent
itself, Unix socket included, so it can't run beside ebooks_agent.py.
"""

import argparse
import logging
import sys
from pathlib import Path

from agent import EbooksAgent
from config import ConfigError, load_config
from tui.app import EbooksApp
from unix_socket import SocketUnavailableError, claim_socket_path


def main(argv=None):
    parser = argparse.ArgumentParser(description="Search #ebooks and request books, in the terminal.")
    parser.add_argument("--config", type=Path, metavar="PATH",
                        help="config file to use (default: config.toml next to this script)")
    args = parser.parse_args(argv)

    # The TUI owns the terminal: log lines printed to it would garble the
    # screen. Until there's a log view, drop them (a handler on the root
    # logger also stops Python printing warnings as a last resort).
    logging.getLogger().addHandler(logging.NullHandler())

    try:
        config = load_config(args.config)
    except ConfigError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(2)
    try:
        # Before connecting: another agent may already be running
        socket_path = claim_socket_path()
    except SocketUnavailableError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    EbooksApp(EbooksAgent(config), config.downloads.searches_dir, socket_path).run()


if __name__ == "__main__":
    main()
