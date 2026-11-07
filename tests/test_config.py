"""Configuration: defaults < config.toml < environment < flags, with clear errors."""

from pathlib import Path

import pytest

import config
import dcc
from agent import EbooksAgent
from config import Config, ConfigError, load_config

EXAMPLE = Path(config.__file__).parent / "config.example.toml"


@pytest.fixture(autouse=True)
def no_default_file(tmp_path, monkeypatch):
    """Never read the developer's real config.toml during tests."""
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", tmp_path / "missing.toml")


def write(tmp_path, text, name="config.toml"):
    path = tmp_path / name
    path.write_text(text)
    return path


def test_defaults_without_any_file():
    assert load_config(env={}) == Config()


def test_example_file_matches_defaults():
    # The documented values must be the real defaults. `dir` is relative to
    # the file, which lives next to the code, so it resolves to the same place.
    assert load_config(EXAMPLE, env={}) == Config()


def test_file_overrides_defaults(tmp_path):
    path = write(tmp_path, '[irc]\nnick = "file_nick"\n[search]\noffer_timeout = 90\n')
    cfg = load_config(path, env={})
    assert cfg.irc.nick == "file_nick"
    assert cfg.search.offer_timeout == 90.0
    assert cfg.irc.host == "irc.irchighway.net"  # untouched settings keep defaults


def test_default_file_is_used_when_present(tmp_path, monkeypatch):
    path = write(tmp_path, '[irc]\nnick = "auto"\n')
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", path)
    assert load_config(env={}).irc.nick == "auto"


def test_config_path_from_env(tmp_path):
    path = write(tmp_path, '[irc]\nnick = "via_env"\n', name="other.toml")
    assert load_config(env={"EBOOKS_CONFIG": str(path)}).irc.nick == "via_env"


def test_env_overrides_file(tmp_path):
    path = write(tmp_path, '[irc]\nnick = "file_nick"\n')
    cfg = load_config(path, env={"EBOOKS_IRC_NICK": "env_nick", "EBOOKS_IRC_PORT": "7000",
                                 "EBOOKS_DOWNLOADS_STALL_TIMEOUT": "2.5"})
    assert cfg.irc.nick == "env_nick"
    assert cfg.irc.port == 7000
    assert cfg.downloads.stall_timeout == 2.5


def test_flags_override_env(tmp_path):
    cfg = load_config(env={"EBOOKS_IRC_NICK": "env_nick"},
                      overrides={("irc", "nick"): "flag_nick", ("downloads", "dir"): None})
    assert cfg.irc.nick == "flag_nick"
    assert cfg.downloads.dir == Config().downloads.dir  # None means "flag not given"


def test_relative_dir_in_file_is_relative_to_file(tmp_path, monkeypatch):
    sub = tmp_path / "conf"
    sub.mkdir()
    path = write(sub, '[downloads]\ndir = "books"\n')
    monkeypatch.chdir(tmp_path)
    assert load_config(path, env={}).downloads.dir == (sub / "books").resolve()


def test_relative_dir_from_env_or_flag_is_relative_to_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert load_config(env={"EBOOKS_DOWNLOADS_DIR": "a"}).downloads.dir == tmp_path / "a"
    assert load_config(env={}, overrides={("downloads", "dir"): "b"}).downloads.dir == tmp_path / "b"


def test_home_dir_expanded(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = load_config(env={"EBOOKS_DOWNLOADS_DIR": "~/Books"})
    assert cfg.downloads.dir == tmp_path / "Books"


# --- errors -----------------------------------------------------------------

def test_missing_explicit_file_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml", env={})


def test_invalid_toml(tmp_path):
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(write(tmp_path, "[irc\nnick = "), env={})


@pytest.mark.parametrize("text, match", [
    ('[irc]\nnik = "typo"\n', "unknown setting 'nik' in \\[irc\\]"),
    ('[ircc]\nnick = "x"\n', "unknown section \\[ircc\\]"),
    ('nick = "x"\n', "must be inside a \\[section\\]"),
    ('[irc]\nport = "6697"\n', "irc.port: expected a whole number"),
    ('[irc]\nport = true\n', "irc.port: expected a whole number"),
    ('[irc]\nport = 99999\n', "irc.port: must be 1–65535"),
    ('[irc]\nnick = "9lives"\n', "isn't a valid IRC nick"),
    ('[irc]\nnick = "has space"\n', "isn't a valid IRC nick"),
    ('[irc]\nchannel = "ebooks"\n', "isn't a valid channel"),
    ('[search]\noffer_timeout = 0\n', "must be greater than 0"),
    ('[downloads]\nmax_size_mib = 5000\n', "max_size_mib: must be 1–4095"),
    ('[downloads]\ndisk_headroom_mib = -1\n', "can't be negative"),
    ('[downloads]\ndir = ""\n', "expected a path"),
])
def test_bad_values_in_file(tmp_path, text, match):
    with pytest.raises(ConfigError, match=match):
        load_config(write(tmp_path, text), env={})


def test_bad_env_value_names_the_variable():
    with pytest.raises(ConfigError, match="EBOOKS_IRC_PORT: expected a whole number"):
        load_config(env={"EBOOKS_IRC_PORT": "lots"})


def test_all_errors_reported_together(tmp_path):
    path = write(tmp_path, '[irc]\nport = 0\nnick = "-x"\n')
    with pytest.raises(ConfigError) as e:
        load_config(path, env={})
    assert "irc.port" in str(e.value) and "irc.nick" in str(e.value)


def test_downloads_dir_that_is_a_file(tmp_path):
    (tmp_path / "f").write_text("")
    with pytest.raises(ConfigError, match="not a directory"):
        load_config(env={"EBOOKS_DOWNLOADS_DIR": str(tmp_path / "f")})


def test_unknown_env_var_warns(caplog):
    load_config(env={"EBOOKS_NICK": "oops"})
    assert "EBOOKS_NICK" in caplog.text


# --- applying the config ----------------------------------------------------

def test_dcc_configure_applies_settings(tmp_path, monkeypatch):
    for name in ("DOWNLOADS_DIR", "MAX_DOWNLOAD_BYTES", "_DISK_HEADROOM_BYTES",
                 "CONNECT_TIMEOUT", "STALL_TIMEOUT"):
        monkeypatch.setattr(dcc, name, getattr(dcc, name))  # restored after the test
    cfg = load_config(env={"EBOOKS_DOWNLOADS_DIR": str(tmp_path / "new" / "books"),
                           "EBOOKS_DOWNLOADS_MAX_SIZE_MIB": "5",
                           "EBOOKS_DOWNLOADS_STALL_TIMEOUT": "7"})
    dcc.configure(cfg.downloads)
    assert dcc.DOWNLOADS_DIR == tmp_path / "new" / "books"
    assert dcc.DOWNLOADS_DIR.is_dir()  # created
    assert dcc.MAX_DOWNLOAD_BYTES == 5 * 1024 * 1024
    assert dcc.STALL_TIMEOUT == 7.0
    assert dcc.safe_download_path("x.epub").parent == dcc.DOWNLOADS_DIR


def test_importing_dcc_creates_nothing(tmp_path):
    tmp_path = tmp_path / "clean"  # the autouse downloads fixture already uses tmp_path
    tmp_path.mkdir()
    # The downloads dir used to be created as an import side effect. Import a
    # copy of the modules from an empty directory and check nothing appears.
    import shutil
    import subprocess
    import sys

    for name in ("dcc.py", "config.py"):
        shutil.copy(Path(dcc.__file__).parent / name, tmp_path / name)
    subprocess.run([sys.executable, "-c", "import dcc"], cwd=tmp_path, check=True)
    assert not (tmp_path / "downloads").exists()


def test_agent_uses_config():
    import asyncio
    cfg = load_config(env={"EBOOKS_IRC_NICK": "cfg_nick", "EBOOKS_IRC_HOST": "irc.example.net",
                           "EBOOKS_IRC_CHANNEL": "#books", "EBOOKS_SEARCH_OFFER_TIMEOUT": "5"})

    async def go():
        return EbooksAgent(cfg)._build_bot()

    bot = asyncio.run(go())
    assert bot.config.nick == "cfg_nick"
    assert bot.config.host == "irc.example.net"
    assert bot.config.ssl is True  # always, whatever the config says
    p = bot.get_plugin(__import__("plugin").EbooksPlugin)
    assert p.channel == "#books"
    assert p.offer_timeout == 5.0
