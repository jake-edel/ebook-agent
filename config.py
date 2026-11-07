"""Configuration loading.

Settings come from four layers, each overriding the one before:

1. Built-in defaults (the dataclasses below)
2. A TOML file: --config PATH, else $EBOOKS_CONFIG, else config.toml next to this file
3. Environment variables named EBOOKS_<SECTION>_<KEY>, e.g. EBOOKS_IRC_NICK
4. Command-line flags (see ebooks_agent.py)

See config.example.toml for every setting and what it does.
"""

import logging
import os
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, get_type_hints

log = logging.getLogger(__name__)

PROJECT_DIR = Path(__file__).parent.resolve()
DEFAULT_CONFIG_PATH = PROJECT_DIR / "config.toml"
ENV_PREFIX = "EBOOKS_"
CONFIG_PATH_ENV = "EBOOKS_CONFIG"

MIB = 1024 * 1024


class ConfigError(Exception):
    """A config file, environment variable or flag has an invalid value."""


@dataclass(frozen=True)
class IrcConfig:
    host: str = "irc.irchighway.net"
    # TLS port. TLS is always on (see agent.py), so this must be a TLS port.
    port: int = 6697
    nick: str = "pt_bookbot"
    channel: str = "#ebooks"
    # Seconds to wait for the server connection and channel join at startup
    connect_timeout: float = 30.0


@dataclass(frozen=True)
class SearchConfig:
    # Seconds to wait for a bot to make a DCC offer once a search has its turn
    offer_timeout: float = 60.0


@dataclass(frozen=True)
class DownloadsConfig:
    dir: Path = PROJECT_DIR / "downloads"
    # Largest file accepted, over DCC or extracted from a zip
    max_size_mib: int = 100
    # Free disk space that must remain after a download
    disk_headroom_mib: int = 512
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
    search: SearchConfig = field(default_factory=SearchConfig)
    downloads: DownloadsConfig = field(default_factory=DownloadsConfig)


# --- loading ----------------------------------------------------------------

def _sections() -> dict[str, type]:
    """Map each TOML section name to its dataclass, e.g. "irc" -> IrcConfig."""
    return get_type_hints(Config)


def _field_types(section_cls: type) -> dict[str, type]:
    """Map each setting in a section to its type, e.g. "port" -> int."""
    return get_type_hints(section_cls)


def _coerce(value: Any, typ: type, where: str, base_dir: Path, from_text: bool) -> Any:
    """Check (and for env vars/flags, parse) one value against the field's type.

    `from_text` is True for environment variables and flags, which are always
    strings; TOML values already arrive with their real type.
    """
    def bad(expected: str):
        return ConfigError(f"{where}: expected {expected}, got {value!r}")

    if typ is str:
        if not isinstance(value, str):
            raise bad("a string")
        return value

    if typ is int:
        if from_text:
            try:
                return int(value)
            except ValueError:
                raise bad("a whole number") from None
        # bool is a subclass of int in Python, so `true` would sneak through
        if not isinstance(value, int) or isinstance(value, bool):
            raise bad("a whole number")
        return value

    if typ is float:
        if from_text:
            try:
                return float(value)
            except ValueError:
                raise bad("a number") from None
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise bad("a number")
        return float(value)

    if typ is Path:
        if not isinstance(value, str) or not value.strip():
            raise bad("a path")
        path = Path(value).expanduser()
        # Relative paths are relative to the config file (for TOML values)
        # or the current directory (for env vars and flags)
        return (base_dir / path).resolve()

    raise TypeError(f"Unsupported config type {typ!r}")  # programming error


def _apply(cfg: Config, section: str, key: str, value: Any, *,
           where: str, base_dir: Path, from_text: bool) -> Config:
    """Return a copy of `cfg` with one setting changed."""
    typ = _field_types(_sections()[section])[key]
    value = _coerce(value, typ, where, base_dir, from_text)
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
                         base_dir=path.parent, from_text=False)
    return cfg


def env_var_name(section: str, key: str) -> str:
    return f"{ENV_PREFIX}{section}_{key}".upper()


def _load_env(cfg: Config, env: Mapping[str, str]) -> Config:
    expected = {CONFIG_PATH_ENV}
    for section, section_cls in _sections().items():
        for key in _field_types(section_cls):
            name = env_var_name(section, key)
            expected.add(name)
            if name in env:
                cfg = _apply(cfg, section, key, env[name], where=name,
                             base_dir=Path.cwd(), from_text=True)
    # Warn (don't fail) about likely typos, e.g. EBOOKS_NICK instead of EBOOKS_IRC_NICK
    for name in sorted(env):
        if name.startswith(ENV_PREFIX) and name not in expected:
            log.warning("Ignoring unknown environment variable %s", name)
    return cfg


def _config_path(path: Path | None, env: Mapping[str, str]) -> tuple[Path, bool]:
    """Which config file to read, and whether it was asked for explicitly."""
    if path is not None:
        return path, True
    if env.get(CONFIG_PATH_ENV):
        return Path(env[CONFIG_PATH_ENV]).expanduser(), True
    return DEFAULT_CONFIG_PATH, False


def load_config(
    path: Path | None = None,
    env: Mapping[str, str] | None = None,
    overrides: Mapping[tuple[str, str], Any] | None = None,
) -> Config:
    """Build the effective configuration from all four layers.

    `path` is an explicit config file (the --config flag). `overrides` maps
    (section, key) to a string value, for command-line flags.
    Raises ConfigError with a readable message on any problem.
    """
    env = os.environ if env is None else env
    cfg = Config()

    file_path, explicit = _config_path(path, env)
    if file_path.is_file():
        cfg = _load_file(cfg, file_path)
        log.info("Loaded config from %s", file_path)
    elif explicit:
        # A file you asked for by name must exist; the default one is optional
        raise ConfigError(f"Config file not found: {file_path}")

    cfg = _load_env(cfg, env)

    for (section, key), value in (overrides or {}).items():
        if value is not None:
            cfg = _apply(cfg, section, key, value, where=f"--{key}",
                         base_dir=Path.cwd(), from_text=True)

    _validate(cfg)
    return cfg


# --- validation -------------------------------------------------------------

# RFC 2812 nick characters. Servers cap the length (IRCHighway: 30).
_NICK_RE = re.compile(r"^[A-Za-z\[\]\\`_^{|}][A-Za-z0-9\[\]\\`_^{|}-]{0,29}$")
# Channels start with # or &, and can't contain spaces, commas or control chars
_CHANNEL_RE = re.compile(r"^[#&][^\s,\x00-\x1f]{1,49}$")
# DCC acks are 32-bit byte counts, so a transfer must stay under 4 GiB
_MAX_SIZE_MIB_LIMIT = 4095


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
    if not _CHANNEL_RE.match(irc.channel):
        errors.append(f"irc.channel: {irc.channel!r} isn't a valid channel (e.g. #ebooks)")

    for name, value in [
        ("irc.connect_timeout", irc.connect_timeout),
        ("search.offer_timeout", cfg.search.offer_timeout),
        ("downloads.connect_timeout", dl.connect_timeout),
        ("downloads.stall_timeout", dl.stall_timeout),
    ]:
        if not value > 0:
            errors.append(f"{name}: must be greater than 0, got {value}")

    if not 1 <= dl.max_size_mib <= _MAX_SIZE_MIB_LIMIT:
        errors.append(f"downloads.max_size_mib: must be 1–{_MAX_SIZE_MIB_LIMIT}, got {dl.max_size_mib}")
    if dl.disk_headroom_mib < 0:
        errors.append(f"downloads.disk_headroom_mib: can't be negative, got {dl.disk_headroom_mib}")
    if dl.dir.exists() and not dl.dir.is_dir():
        errors.append(f"downloads.dir: {dl.dir} exists and is not a directory")

    if errors:
        raise ConfigError("Invalid configuration:\n  " + "\n  ".join(errors))
