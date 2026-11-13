"""The post-download hook: a command, set in the config, run on each finished book.

It's for moving books somewhere else (another directory, or another machine
with rsync), so it runs only once the book is complete under its final name.
Searches don't trigger it.

The command is an argv list, run without a shell. The book's path replaces
any item that is exactly "{path}" (rsync needs it before the destination),
or is added as the last argument if there's none; it's also in $EBOOK_PATH.
Filenames come from IRC bots, so they must never be pasted into a shell
string: a book named `$(rm -rf ~).epub` would run. The path is absolute, so
it can't be mistaken for an option either.

The hook runs unattended, often under the TUI, so it gets no terminal: stdin
is /dev/null (an ssh password prompt fails instead of hanging) and its output
goes to the log. A failed hook is logged but doesn't fail the request: the
book did download, and it stays where it was saved. Frontends see how it went
in Request.hook and hook_error.
"""

import asyncio
import logging
import os
import shlex
from pathlib import Path

log = logging.getLogger(__name__)

# Keep the log readable if a hook is chatty
_MAX_OUTPUT = 4000
# And a failure's reason short enough for a status bar
_MAX_REASON = 200


async def run_hook(command: tuple[str, ...], path: Path, timeout: float) -> str | None:
    """Run `command` on `path`: None if it exited 0 within `timeout` seconds,
    else a short reason, for frontends ("exit 23: <its last line of output>").

    Never raises for the hook's own failures (missing command, non-zero exit,
    timeout): they're logged, and returned. If this is cancelled, the hook is killed.
    """
    argv = hook_argv(command, path)
    log.info("Running hook: %s", shlex.join(argv))
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "EBOOK_PATH": str(path)},
            # Its own session, so it can't read from or signal our terminal,
            # and ssh won't try to prompt on /dev/tty
            start_new_session=True,
        )
    except OSError as e:
        log.warning("Hook failed to start for %s: %s", path.name, e)
        return f"couldn't start: {e}"

    try:
        async with asyncio.timeout(timeout):
            output, _ = await proc.communicate()
    except TimeoutError:
        log.warning("Hook timed out after %gs for %s; killed it", timeout, path.name)
        return f"timed out after {timeout:g}s"
    finally:
        if proc.returncode is None:  # timed out or cancelled
            proc.kill()
            await asyncio.shield(proc.wait())

    text = output.decode(errors="replace").strip()
    # The last line is usually the error (rsync's, ssh's): for frontends
    last = text.splitlines()[-1].strip()[:_MAX_REASON] if text else ""
    if len(text) > _MAX_OUTPUT:
        text = text[:_MAX_OUTPUT] + "\n[... output truncated]"
    if proc.returncode == 0:
        log.info("Hook done for %s%s", path.name, f":\n{text}" if text else "")
        return None
    log.warning("Hook exited with status %d for %s%s", proc.returncode, path.name,
                f":\n{text}" if text else "")
    return f"exit {proc.returncode}: {last}" if last else f"exit {proc.returncode}"


PLACEHOLDER = "{path}"


def hook_argv(command: tuple[str, ...], path: Path) -> list[str]:
    """The argv to run: "{path}" items replaced by `path`, else `path` appended."""
    if PLACEHOLDER in command:
        return [str(path) if arg == PLACEHOLDER else arg for arg in command]
    return [*command, str(path)]
