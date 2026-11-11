# TODO

Outstanding work, roughly in priority order. The backend is done: what it offers frontends is in [`docs/core-interface.md`](docs/core-interface.md), and its history is in the git log.

## Next: the TUI

The interactive, Neovim-inspired frontend. Design and phases are in [`docs/tui-plan.md`](docs/tui-plan.md), starting with phase 1.

## Robustness / polish

- [ ] **Pin dependencies**
  - **Problem:** `requirements.txt` (`irc3`) and `requirements-dev.txt` (`pytest`) don't specify versions.
  - **Fix:** pin known-good versions (`irc3==1.1.12` at the time of writing), or add a lock file.

- [ ] **irc3 will break on Python 3.16**
  - **Problem:** irc3 1.1.12 calls `asyncio.iscoroutinefunction` (in `irc3/dec.py` and `irc3/utils.py`), deprecated since Python 3.14 and removed in 3.16 (the venv is on 3.13.5 at the time of writing). The warning is hidden in `pytest.ini`.
  - **Fix:** watch for an irc3 release that fixes it, or patch it locally, before upgrading Python.

- [ ] **CI**
  - **Fix:** add a GitHub Actions workflow that runs `pytest` on every push.

## Feature ideas

- [ ] **Fully automatic `@search` → `!Bot` flow**
  - **Idea:** a non-interactive mode for scripts and the socket: send `@search`, parse the results (`results.py`), pick the best match (prefer `.epub`, a bot that's in the channel, a reasonable size), then request it.
  - **Relation to the TUI:** the TUI is the version where you make that choice yourself.

## Known limitations / accepted risks

These were decided on purpose. Revisit them if the threat model changes.

- **The IRC TLS connection isn't verified.** IRCHighway round-robins across 5 or more servers, each with its own self-signed cert, and doesn't publish the list, so pinning would break at random. TLS (1.2 or later) still protects against passive observers, and each connection logs the server IP and cert fingerprint.
- **DCC transfers are plaintext.** That's how the protocol works. An active attacker on your network could tamper with downloads no matter how the IRC connection is protected.
- **The sender check for searches is "any member of `#ebooks`".** While a search waits for its offer, any of the roughly 600 channel members can make one (to a public IP only, within the size limits).
  - Book requests (`!Bot …`) are already strict: only that bot is accepted.
  - Could tighten further to voiced/op users (`+v`/`@`), or a list of known search bots.
- **A bot's trigger is assumed to be its nick.** `!Bsk …` only accepts an offer from `Bsk`. A bot answering under a different nick would time out.
- **Passive (reverse) DCC isn't supported.** Such offers are refused with the reason.
- **The download size limit is 100 MiB** (`downloads.max_size_mib` in the config). Raise it if large illustrated PDFs get refused.
- **Downloaded files aren't checked.** A file named `.epub` could contain anything. Open downloads with appropriate care.
- **Cancelling during zip extraction doesn't stop it.** Extraction runs in a thread, so the book can still appear after its request shows as cancelled.
