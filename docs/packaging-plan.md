# Packaging plan

Make the project installable as a uv tool, so one command (`uv tool install git+ssh://…`, or `uv tool install .` from a checkout) puts `ebooks` on your `PATH`, and it runs from any directory, on any Unix machine, with no venv to activate.

## Goal

Open a terminal on whatever machine, type `ebooks`, and get going. Setting up a new machine should take two commands:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # uv: one static binary, which fetches Python too
uv tool install git+ssh://…/ebooks-cli             # or a copied wheel, see "Installing"
```

## Why a uv tool, not a standalone binary

A frozen binary (PyInstaller, Nuitka) was considered and rejected:

- **One build per OS and architecture.** A Linux build is also tied to the glibc it was built against, so a binary from Debian 13 may not run on an older distro. That means a build matrix, for a project nobody else installs.
- **Both main dependencies fight freezing.** irc3 loads its plugins by dotted name (`"irc3.plugins.core"`, `"irc3.plugins.userlist"` in `server/agent.py`), and Textual imports its widgets lazily and needs its `.tcss` files. Under PyInstaller both mean hand-kept lists of hidden imports and data files, which break quietly when a dependency changes.
- **Nothing to gain.** Every dependency is pure Python (irc3, venusian, docopt, textual, rich, pygments, markdown-it, platformdirs), so one `py3-none-any` wheel already runs everywhere. uv downloads a suitable Python itself, so the machine's Python doesn't matter.

For a one-off run without installing, `uvx --from git+ssh://…/ebooks-cli ebooks` works too.

**Unix only**, whatever the packaging: the agent's interface is a Unix socket. macOS works (with no `$XDG_RUNTIME_DIR`, `socket_dir()` falls back to the temp dir); Windows doesn't.

## Background

Today the project only runs from a checkout:

- **No package.** `config.py`, `core.py`, `results.py`, `server/` and `tui/` are top-level modules, found because the scripts (`ebooks_agent.py`, `ebooks_tui.py`) sit next to them, and because of `pythonpath = .` in `pytest.ini`. Installed as they are, they'd be top-level modules named `config`, `core` and `server` in the tool's venv: generic names that clash easily, and nothing says they belong to this project.
- **Paths come from the source tree.** `config.py` sets `PROJECT_DIR = Path(__file__).parent`, and every default path hangs off it: `config.toml`, `downloads/`, `searches/` and `agent.log`. Installed, that's inside uv's tool venv (`~/.local/share/uv/tools/…/site-packages`). Your searches and books would live there, and `uv tool upgrade` or a reinstall would delete them.
- **Two scripts.** `ebooks_tui.py` runs the TUI (with the agent in-process) and `ebooks_agent.py` runs the agent headless. They can't run side by side anyway (both claim the socket), so they're two modes of one program.
- **No packaging metadata.** Dependencies are in `requirements.txt` and `requirements-dev.txt`, unpinned apart from `textual`. There's no `pyproject.toml`, no version, and no entry points.

What already works in our favour:

- Both scripts have a `main(argv=None)`, so an entry point is a one-line change.
- With no config file, everything runs on defaults. A missing searches folder means an empty sidebar, and the downloads folder and the log's folder are created when first needed (`server/dcc.py`, `server/logfile.py`). So a first run needs no setup.
- Relative paths in a config file already resolve against that file (`config.py`, `_apply`), not the current directory.
- irc3 only loads its own plugins by dotted name (`server/agent.py`); ours is passed as a class. Log names come from `__name__`. Nothing breaks from moving modules except the imports themselves.
- `tui/app.py` finds `app.tcss` relative to the module (`CSS_PATH`), so it keeps working from site-packages, as long as the file is in the wheel.
- The Unix socket already lives in `$XDG_RUNTIME_DIR`, not the project.

## Decisions

- **One package, `ebooks`, in a `src/` layout** (`src/ebooks/…`). With `src/`, tests can't pass by importing the checkout's files instead of the installed package. That's the mistake `pythonpath = .` invites, and uv's build backend expects this layout by default.
- **One command, `ebooks`.** The TUI is the default; `ebooks --headless` is today's `ebooks_agent.py` (logs to the terminal, input over the socket); `ebooks --socket-path` prints the socket's path and exits. This was TUI plan stage 6's "the TUI as the default with `--headless`", pulled forward into stage 2a here, so the program is renamed once rather than twice.
- **XDG locations for everything outside the package**, read from `$XDG_CONFIG_HOME`, `$XDG_DATA_HOME` and `$XDG_STATE_HOME`, with the usual fallbacks. Hand-written (about 15 lines), not `platformdirs`: the agent is Unix-only anyway (the socket), so there's no need for a dependency.

  | What | Default |
  |---|---|
  | config | `~/.config/ebooks/config.toml` |
  | searches | `~/.local/share/ebooks/searches/` |
  | downloads | `~/Downloads/ebooks/` |
  | log | `~/.local/state/ebooks/agent.log` |

  Downloads go to `~/Downloads/ebooks/`, not the data folder: on a fresh machine without `on_complete` set up yet, that's where you'd look for a book. Searches are the app's own data, so they stay under `~/.local/share`. Plain `~/Downloads`, not `$XDG_DOWNLOAD_DIR` from `user-dirs.dirs`: that file isn't on every machine (servers, macOS), and `[downloads] dir` overrides it anyway.
- **The distribution is `ebooks-cli`**, the package `ebooks`. Not published to PyPI: `ebooks` is very likely taken there, and nobody else needs it.
- **Install from git or from a wheel.** `uv tool install git+ssh://…` (a private repo works over SSH) on machines with your keys; elsewhere, `uv build` and copy the wheel over. Both are in the README. No tags or release process: `main` is the release, and a wheel's version is enough to tell builds apart.
- **Pinned dependencies, and a Python range:** `irc3==1.1.12`, `textual==8.2.8`, `requires-python = ">=3.11,<3.16"`. 3.11 for `tomllib`; below 3.16 because irc3 1.1.12 breaks there (see `TODO.md`), so `uv tool install` won't choose a Python it can't run on. `uv.lock` is committed.
- **No migration code.** There's one existing install (this checkout), so moving its files over is a documented one-off, not something the app does on startup.

## Design

### Layout

```
pyproject.toml
uv.lock
src/ebooks/
    __init__.py
    __main__.py         # new: `python -m ebooks`, calls cli.main()
    cli.py              # new: was ebooks_agent.py and ebooks_tui.py
    config.py
    core.py
    results.py
    paths.py            # new: the XDG folders
    example-config.toml # was config.example.toml, now shipped in the package
    server/…            # as now
    tui/…               # as now, app.tcss included
tests/…
```

`python -m ebooks` works the same as `ebooks`, for development and for `tests/test_main.py`, which runs `--socket-path` as a subprocess.

### The command

`cli.py` has one `main(argv=None)` and one parser:

```
ebooks [--config PATH] [--headless | --socket-path | --init-config]
```

- `--headless`, `--socket-path` and `--init-config` are mutually exclusive (an argparse group).
- The headless path is `ebooks_agent.py`'s code as it is (`_serve`, `_main`, `_run`); the TUI path is `ebooks_tui.py`'s `main` body.
- **Textual is imported only on the TUI path** (inside the function, not at the top of `cli.py`), so `--headless` and `--socket-path` start quickly and never load it. `check_theme` moves with it. A test checks that `ebooks --socket-path` doesn't import `textual`, as `test_config.py` does today for `config.py`.
- Logging setup differs by mode, as now: `--headless` logs to stderr (`basicConfig`), the TUI only to the log file (a `NullHandler` until it's open).
- The log's session line names the mode: `session start: ebooks pid …` for the TUI, `ebooks --headless pid …` for headless.

### pyproject.toml

```toml
[project]
name = "ebooks-cli"
version = "0.1.0"
description = "Search IRCHighway's #ebooks and download books over DCC"
readme = "README.md"
requires-python = ">=3.11,<3.16"
dependencies = [
    "irc3==1.1.12",
    "textual==8.2.8",
]

[project.scripts]
ebooks = "ebooks.cli:main"

[dependency-groups]
dev = ["pytest"]

[build-system]
requires = ["uv_build>=0.8,<0.9"]
build-backend = "uv_build"

[tool.pytest.ini_options]
testpaths = ["tests"]
filterwarnings = [
    # irc3 uses asyncio.iscoroutinefunction, deprecated in Python 3.14: not our code
    "ignore:'asyncio.iscoroutinefunction' is deprecated:DeprecationWarning:irc3",
]
```

`pytest.ini`, `requirements.txt` and `requirements-dev.txt` go: `uv sync` installs the project (editable) plus the dev group, and `uv run pytest` runs against it.

### Paths

`ebooks/paths.py` gives `config_dir()`, `data_dir()` and `state_dir()`: `$XDG_…_HOME/ebooks` if the variable is set to an absolute path (the spec says to ignore a relative one), else `~/.config/ebooks`, `~/.local/share/ebooks` and `~/.local/state/ebooks`. Plus `downloads_dir()`: `~/Downloads/ebooks`.

In `config.py`:

- `PROJECT_DIR` goes. `DEFAULT_CONFIG_PATH` becomes `config_dir() / "config.toml"`.
- The path defaults become `field(default_factory=…)` instead of values worked out at import time, so a test that sets `XDG_*` (or `HOME`) sees the change.
- The docstrings and `--config` help stop saying "config.toml next to this file/script". They name the real default, and `--help` shows the resolved path.

### Getting started

There's no need to create a config: the defaults work. But with the example out of the repo root, there needs to be a way to see it:

- `ebooks --init-config` writes the shipped `example-config.toml` to the default config path (or to `--config PATH`), refusing to overwrite an existing file, and prints where it went.
- It's read with `importlib.resources`, so it works from the wheel.

### Installing

The README's *Install* section, first:

```bash
# uv, if the machine doesn't have it
curl -LsSf https://astral.sh/uv/install.sh | sh

# from git (needs your SSH key on this machine)
uv tool install git+ssh://git@github.com/…/ebooks-cli
uv tool upgrade ebooks-cli        # later

# or from a wheel, built anywhere and copied over
uv build                          # in a checkout: dist/ebooks_cli-0.1.0-py3-none-any.whl
uv tool install ./ebooks_cli-0.1.0-py3-none-any.whl
```

Then *Development*: `uv sync`, `uv run ebooks`, `uv run pytest`, and `uv tool install -e .` to have `ebooks` on the `PATH` run the checkout.

### Tests

- An autouse fixture in `tests/conftest.py` points `XDG_CONFIG_HOME`, `XDG_DATA_HOME` and `XDG_STATE_HOME` at `tmp_path`, and `HOME` too (for `~/Downloads`), so no test can read your real config or write to your real folders.
- `tests/test_main.py` runs `[sys.executable, "-m", "ebooks", "--socket-path"]` instead of the script's path, and imports `ebooks.cli` for `_serve` and `_main`.
- New: the XDG defaults and fallbacks (set, unset, relative); the flags are mutually exclusive; `--socket-path` doesn't import Textual; `--init-config` writes the example, refuses to overwrite, and the file it writes loads cleanly; `app.tcss` and `example-config.toml` are in a built wheel (`uv build`, then list the wheel's files).

## Stages

| # | Stage | Status |
|---|---|---|
| 1 | **pyproject.toml, without moving anything.** `pyproject.toml` with the pinned dependencies, the dev group and pytest's settings, as a non-package project for now (`[tool.uv] package = false`, no `[project.scripts]`). Commit `uv.lock`. `requirements*.txt` and `pytest.ini` go. README: `uv sync` and `uv run` replace the venv instructions. Closes "Pin dependencies" in `TODO.md`. | done |
| 2a | **One command.** `ebooks_agent.py` becomes `ebooks.py` (`git mv`, so history follows it), and `ebooks_tui.py` is folded into it: one parser, `--headless` and `--socket-path` mutually exclusive, Textual imported on the TUI path only (with a test), the session line's program names changed (`ebooks`, `ebooks --headless`). Still top-level and run with `uv run ebooks.py`. README, docstrings and the TUI plan updated | done |
| 2b | **The package.** `git mv` everything into `src/ebooks/` (one commit with only the moves, so history follows the files; `ebooks.py` becomes `cli.py`). Imports become `ebooks.…` (or relative within `server/` and `tui/`); add `__main__.py`. Add the `[build-system]` and `[project.scripts]` sections and drop `package = false`. `tests/test_main.py` runs `python -m ebooks`. Update the references to `ebooks.py` in docstrings, the README and the docs. Defaults still point into the source tree, so behaviour doesn't change yet. | |
| 3 | **XDG paths.** `ebooks/paths.py`, the `config.py` defaults, the conftest fixture, `--help` showing the real paths. README: where things live now, and how to move an existing checkout's files (below). | |
| 4 | **`--init-config`.** Ship `example-config.toml` in the package, and add the flag. | |
| 5 | **Install and check.** `uv build` and check the wheel's contents; `uv tool install .` and run `ebooks` from another directory; install the built wheel on a second machine (or a fresh container); `uv tool install -e .` for development. README: the *Install* section first, with the checkout setup moved under *Development*. | |

Each stage leaves the tests passing and the app working from the checkout (`uv run ebooks` from stage 2b on).

### Moving this checkout's files (after stage 3)

```bash
mkdir -p ~/.config/ebooks ~/.local/share/ebooks ~/.local/state/ebooks ~/Downloads/ebooks
mv config.toml ~/.config/ebooks/
mv searches ~/.local/share/ebooks/
mv downloads/* ~/Downloads/ebooks/ && rmdir downloads
mv agent.log* ~/.local/state/ebooks/
```

Check `config.toml` first: a relative path in it (`dir = "downloads"`) now resolves against `~/.config/ebooks/`, so make it absolute, or remove it to get the new default.

## Open questions

None left. Settled in review:

1. **Downloads folder:** `~/Downloads/ebooks/` (see *Decisions*).
2. **One command or two:** one, `ebooks` with `--headless` (stage 2a).
3. **Where it installs from:** git over SSH, or a copied wheel; no tagged releases.
