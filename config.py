"""Configuration loading.

Settings come from the built-in defaults (the dataclasses below), overridden
by a TOML file: --config PATH, else config.toml next to this file.

See config.example.toml for every setting and what it does.
"""

import logging
import re
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, get_type_hints

log = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).parent.resolve()
DEFAULT_CONFIG_PATH = PROJECT_DIR / "config.toml"

MIB = 1024 * 1024


class ConfigError(Exception):
    """The config file is missing, malformed, or has an invalid value."""


@dataclass(frozen=True)
class IrcConfig:
    host: str = "irc.irchighway.net"
    # TLS port. TLS is always on (see agent.py), so this must be a TLS port.
    port: int = 6697
    nick: str = "anjay"
    # What a /whois on us shows, besides the nick: the "user" part of
    # nick!user@host, and the free-text real name. Left to irc3, these would
    # announce us as "Irc bot based on irc3"
    username: str = "anjay"
    realname: str = "andrew_jacobs"
    channel: str = "#ebooks"
    # Seconds to wait for the server connection and channel join at startup
    connect_timeout: float = 30.0
    # Log messages addressed to us (DMs, bot replies, server notices)
    show_private_messages: bool = True


@dataclass(frozen=True)
class DownloadsConfig:
    # Where books are saved
    dir: Path = PROJECT_DIR / "downloads"
    # Where @search results files are saved, kept apart from the books
    searches_dir: Path = PROJECT_DIR / "searches"
    # Largest file accepted, over DCC or extracted from a zip
    max_size_mib: int = 100
    # Free disk space that must remain after a download
    disk_headroom_mib: int = 512
    # Seconds a bot has to make its DCC offer, from when the search or book
    # request is sent to the channel
    offer_timeout: float = 60.0
    # Book requests one bot may have from us at once, from sending the line
    # until its file has arrived. More wait their turn, to stay polite.
    max_requests_per_bot: int = 2
    # Seconds to wait for a DCC sender to accept our connection
    connect_timeout: float = 15.0
    # Give up on a transfer after this many seconds without data
    stall_timeout: float = 30.0

    @property
    def max_size_bytes(self) -> int:
        return self.max_size_mib * MIB

    @property
    def disk_headroom_bytes(self) -> int:
        return self.disk_headroom_mib * MIB


@dataclass(frozen=True)
class Config:
    irc: IrcConfig = field(default_factory=IrcConfig)
    downloads: DownloadsConfig = field(default_factory=DownloadsConfig)


# --- loading ----------------------------------------------------------------

def _sections() -> dict[str, type]:
    """Map each TOML section name to its dataclass, e.g. "irc" -> IrcConfig."""
    return get_type_hints(Config)


def _field_types(section_cls: type) -> dict[str, type]:
    """Map each setting in a section to its type, e.g. "port" -> int."""
    return get_type_hints(section_cls)


def _coerce(value: Any, typ: type, where: str, base_dir: Path) -> Any:
    """Check one TOML value against the field's type."""
    def bad(expected: str):
        return ConfigError(f"{where}: expected {expected}, got {value!r}")

    if typ is str:
        if not isinstance(value, str):
            raise bad("a string")
        return value

    if typ is bool:
        if not isinstance(value, bool):
            raise bad("true or false")
        return value

    if typ is int:
        # bool is a subclass of int in Python, so `true` would sneak through
        if not isinstance(value, int) or isinstance(value, bool):
            raise bad("a whole number")
        return value

    if typ is float:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise bad("a number")
        return float(value)

    if typ is Path:
        if not isinstance(value, str) or not value.strip():
            raise bad("a path")
        path = Path(value).expanduser()
        # Relative paths are relative to the config file
        return (base_dir / path).resolve()

    raise TypeError(f"Unsupported config type {typ!r}")  # programming error


def _apply(cfg: Config, section: str, key: str, value: Any, *, where: str, base_dir: Path) -> Config:
    """Return a copy of `cfg` with one setting changed."""
    typ = _field_types(_sections()[section])[key]
    value = _coerce(value, typ, where, base_dir)
    new_section = replace(getattr(cfg, section), **{key: value})
    return replace(cfg, **{section: new_section})


def _load_file(cfg: Config, path: Path) -> Config:
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{path}: invalid TOML: {e}") from None

    sections = _sections()
    for section, values in data.items():
        # e.g. `nick = "x"` written above any [section] header
        if not isinstance(values, dict):
            raise ConfigError(
                f"{path}: '{section}' must be inside a [section] "
                f"(one of: {', '.join(sections)})"
            )
        if section not in sections:
            raise ConfigError(
                f"{path}: unknown section [{section}] "
                f"(expected one of: {', '.join(sections)})"
            )
        known = set(_field_types(sections[section]))
        for key, value in values.items():
            # Catch typos like "nik = ..." instead of silently ignoring them
            if key not in known:
                raise ConfigError(
                    f"{path}: unknown setting '{key}' in [{section}] "
                    f"(expected one of: {', '.join(sorted(known))})"
                )
            cfg = _apply(cfg, section, key, value, where=f"{path}: {section}.{key}",
                         base_dir=path.parent)
    return cfg


def load_config(path: Path | None = None) -> Config:
    """Build the effective configuration: the defaults, overridden by a TOML file.

    `path` is an explicit config file (the --config flag), which must exist.
    Without one, config.toml next to this file is used if it's there.
    Raises ConfigError with a readable message on any problem.
    """
    cfg = Config()
    file_path = DEFAULT_CONFIG_PATH if path is None else path.expanduser()
    if file_path.is_file():
        cfg = _load_file(cfg, file_path)
        log.info("Loaded config from %s", file_path)
    elif path is not None:
        # A file you asked for by name must exist; the default one is optional
        raise ConfigError(f"Config file not found: {file_path}")

    _validate(cfg)
    return cfg


# --- validation -------------------------------------------------------------

# RFC 2812 nick characters. Servers cap the length (IRCHighway: 30).
_NICK_RE = re.compile(r"^[A-Za-z\[\]\\`_^{|}][A-Za-z0-9\[\]\\`_^{|}-]{0,29}$")
# The user part of nick!user@host: no spaces, "@" or control chars. Servers
# cap it (commonly at 10) and may prefix "~" when we run no identd.
_USERNAME_RE = re.compile(r"^[^\s@\x00-\x1f\x7f]{1,10}$")
_MAX_REALNAME = 50
# Channels start with # or &, and can't contain spaces, commas or control chars
_CHANNEL_RE = re.compile(r"^[#&][^\s,\x00-\x1f]{1,49}$")
# DCC acks are 32-bit byte counts, so a transfer must stay under 4 GiB
_MAX_SIZE_MIB_LIMIT = 4095
# More than this at once from one bot is just greedy
_MAX_REQUESTS_PER_BOT = 10


def _validate(cfg: Config):
    errors = []
    irc, dl = cfg.irc, cfg.downloads

    if not irc.host or any(c.isspace() for c in irc.host):
        errors.append(f"irc.host: invalid hostname {irc.host!r}")
    if not 1 <= irc.port <= 65535:
        errors.append(f"irc.port: must be 1–65535, got {irc.port}")
    if not _NICK_RE.match(irc.nick):
        errors.append(
            f"irc.nick: {irc.nick!r} isn't a valid IRC nick "
            "(letters, digits and []\\`_^{|}-, not starting with a digit or -, max 30)"
        )
    if not _USERNAME_RE.match(irc.username):
        errors.append(
            f"irc.username: {irc.username!r} isn't a valid username "
            "(1–10 characters, no spaces or @)"
        )
    if (not irc.realname.strip() or len(irc.realname) > _MAX_REALNAME
            or any(c in irc.realname for c in "\r\n\x00")):
        errors.append(
            f"irc.realname: {irc.realname!r} must be 1–{_MAX_REALNAME} characters on one line"
        )
    if not _CHANNEL_RE.match(irc.channel):
        errors.append(f"irc.channel: {irc.channel!r} isn't a valid channel (e.g. #ebooks)")

    for name, value in [
        ("irc.connect_timeout", irc.connect_timeout),
        ("downloads.offer_timeout", dl.offer_timeout),
        ("downloads.connect_timeout", dl.connect_timeout),
        ("downloads.stall_timeout", dl.stall_timeout),
    ]:
        if not value > 0:
            errors.append(f"{name}: must be greater than 0, got {value}")

    if not 1 <= dl.max_size_mib <= _MAX_SIZE_MIB_LIMIT:
        errors.append(f"downloads.max_size_mib: must be 1–{_MAX_SIZE_MIB_LIMIT}, got {dl.max_size_mib}")
    if not 1 <= dl.max_requests_per_bot <= _MAX_REQUESTS_PER_BOT:
        errors.append(f"downloads.max_requests_per_bot: must be 1–{_MAX_REQUESTS_PER_BOT}, "
                      f"got {dl.max_requests_per_bot}")
    if dl.disk_headroom_mib < 0:
        errors.append(f"downloads.disk_headroom_mib: can't be negative, got {dl.disk_headroom_mib}")
    for name, path in [("downloads.dir", dl.dir), ("downloads.searches_dir", dl.searches_dir)]:
        if path.exists() and not path.is_dir():
            errors.append(f"{name}: {path} exists and is not a directory")
    if dl.dir == dl.searches_dir:
        errors.append(f"downloads.searches_dir: must differ from downloads.dir ({dl.dir})")

    if errors:
        raise ConfigError("Invalid configuration:\n  " + "\n  ".join(errors))
