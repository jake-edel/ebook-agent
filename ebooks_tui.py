"""Entry point for the TUI, while it's being built (see docs/tui-plan.md).

Stage 1: browse past searches in the searches folder. Nothing connects to
IRC yet; ebooks_agent.py is still the way to search and download.
"""

import argparse
import logging
import sys
from pathlib import Path

from config import ConfigError, load_config
from tui.app import EbooksApp


def main(argv=None):
    parser = argparse.ArgumentParser(description="Browse #ebooks searches in the terminal.")
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
    EbooksApp(config.downloads.searches_dir).run()


if __name__ == "__main__":
    main()
