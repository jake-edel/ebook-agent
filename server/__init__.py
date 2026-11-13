"""The agent side: the IRC connection, DCC transfers, the post-download hook,
the Unix socket and the log file. Frontends (tui/, ebooks_agent.py) use it
through the core interface (core.py), which, with config.py and results.py,
stays at the top level because both sides share it."""
