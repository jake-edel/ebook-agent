"""Configuration: defaults overridden by config.toml, with clear errors."""

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
    assert load_config() == Config()


def test_example_file_matches_defaults():
    # The documented values must be the real defaults. `dir` is relative to
    # the file, which lives next to the code, so it resolves to the same place.
    assert load_config(EXAMPLE) == Config()


def test_file_overrides_defaults(tmp_path):
    path = write(tmp_path, '[irc]\nnick = "file_nick"\n[downloads]\noffer_timeout = 90\n')
    cfg = load_config(path)
    assert cfg.irc.nick == "file_nick"
    assert cfg.downloads.offer_timeout == 90.0
    assert cfg.irc.host == "irc.irchighway.net"  # untouched settings keep defaults


def test_default_file_is_used_when_present(tmp_path, monkeypatch):
    path = write(tmp_path, '[irc]\nnick = "auto"\n')
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", path)
    assert load_config().irc.nick == "auto"


def test_relative_dir_in_file_is_relative_to_file(tmp_path, monkeypatch):
    sub = tmp_path / "conf"
    sub.mkdir()
    path = write(sub, '[downloads]\ndir = "books"\n')
    monkeypatch.chdir(tmp_path)
    assert load_config(path).downloads.dir == (sub / "books").resolve()


def test_home_dir_expanded(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    cfg = load_config(write(tmp_path, '[downloads]\ndir = "~/Books"\n'))
    assert cfg.downloads.dir == tmp_path / "Books"


def test_config_path_with_home_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    write(tmp_path, '[irc]\nnick = "from_home"\n', name="mine.toml")
    assert load_config(Path("~/mine.toml")).irc.nick == "from_home"


# --- errors -----------------------------------------------------------------

def test_missing_explicit_file_is_an_error(tmp_path):
    with pytest.raises(ConfigError, match="not found"):
        load_config(tmp_path / "nope.toml")


def test_invalid_toml(tmp_path):
    with pytest.raises(ConfigError, match="invalid TOML"):
        load_config(write(tmp_path, "[irc\nnick = "))


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
    ('[irc]\nusername = ""\n', "isn't a valid username"),
    ('[irc]\nusername = "two words"\n', "isn't a valid username"),
    ('[irc]\nusername = "me@home"\n', "isn't a valid username"),
    ('[irc]\nusername = "elevenchars"\n', "isn't a valid username"),
    ('[irc]\nrealname = "  "\n', "realname: .* must be 1–50 characters"),
    ('[irc]\nrealname = "line\\nbreak"\n', "realname: .* must be 1–50 characters"),
    (f'[irc]\nrealname = "{"x" * 51}"\n', "realname: .* must be 1–50 characters"),
    ('[downloads]\noffer_timeout = 0\n', "must be greater than 0"),
    ('[downloads]\nmax_size_mib = 5000\n', "max_size_mib: must be 1–4095"),
    ('[downloads]\ndisk_headroom_mib = -1\n', "can't be negative"),
    ('[downloads]\nmax_requests_per_bot = 0\n', "max_requests_per_bot: must be 1–10"),
    ('[downloads]\nmax_requests_per_bot = 11\n', "max_requests_per_bot: must be 1–10"),
    ('[downloads]\ndir = ""\n', "expected a path"),
    ('[log]\nfile = ""\n', "expected a path"),
    ('[log]\nmax_size_mib = 0\n', "log.max_size_mib: must be 1–1024"),
    ('[log]\nbackups = 0\n', "log.backups: must be 1–20"),
    ('[log]\nbackups = 21\n', "log.backups: must be 1–20"),
])
def test_bad_values_in_file(tmp_path, text, match):
    with pytest.raises(ConfigError, match=match):
        load_config(write(tmp_path, text))


def test_username_and_realname_from_file(tmp_path):
    cfg = load_config(write(tmp_path, '[irc]\nusername = "jake"\nrealname = "Jake E."\n'))
    assert (cfg.irc.username, cfg.irc.realname) == ("jake", "Jake E.")


def test_all_errors_reported_together(tmp_path):
    path = write(tmp_path, '[irc]\nport = 0\nnick = "-x"\n')
    with pytest.raises(ConfigError) as e:
        load_config(path)
    assert "irc.port" in str(e.value) and "irc.nick" in str(e.value)


def test_downloads_dir_that_is_a_file(tmp_path):
    (tmp_path / "f").write_text("")
    with pytest.raises(ConfigError, match="not a directory"):
        load_config(write(tmp_path, '[downloads]\ndir = "f"\n'))


def test_log_file_that_is_a_directory(tmp_path):
    (tmp_path / "logs").mkdir()
    with pytest.raises(ConfigError, match="log.file: .* is a directory"):
        load_config(write(tmp_path, '[log]\nfile = "logs"\n'))


def test_log_settings_from_file(tmp_path):
    cfg = load_config(write(tmp_path, '[log]\nfile = "~/x.log"\nmax_size_mib = 2\nbackups = 5\n'))
    assert cfg.log.file == (Path.home() / "x.log").resolve()
    assert (cfg.log.max_size_bytes, cfg.log.backups) == (2 * 1024 * 1024, 5)


def test_config_file_is_the_one_read(tmp_path, monkeypatch):
    assert config.config_file() is None  # no config.toml: defaults only
    path = write(tmp_path, "")
    assert config.config_file(path) == path.resolve()
    monkeypatch.setattr(config, "DEFAULT_CONFIG_PATH", path)
    assert config.config_file() == path.resolve()


# --- applying the config ----------------------------------------------------

def test_downloads_applies_settings(tmp_path):
    cfg = load_config(write(tmp_path, '[downloads]\ndir = "new/books"\n'
                                      'max_size_mib = 5\nstall_timeout = 7\n'))
    dl = dcc.Downloads(cfg.downloads)
    assert not dl.dir.exists()  # nothing touches disk until create_dirs()
    dl.create_dirs()
    assert dl.dir == tmp_path / "new" / "books"
    assert dl.dir.is_dir()
    assert dl.max_bytes == 5 * 1024 * 1024
    assert dl.config.stall_timeout == 7.0
    assert dl.safe_path("x.epub").parent == dl.dir


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


def test_agent_uses_config(tmp_path):
    import asyncio
    cfg = load_config(write(tmp_path, '[irc]\nnick = "cfg_nick"\nhost = "irc.example.net"\n'
                                      'channel = "#books"\n[downloads]\noffer_timeout = 5\n'))

    async def go():
        return EbooksAgent(cfg)._build_bot()

    bot = asyncio.run(go())
    assert bot.config.nick == "cfg_nick"
    assert bot.config.host == "irc.example.net"
    assert bot.config.ssl is True  # always, whatever the config says
    p = bot.get_plugin(__import__("plugin").EbooksPlugin)
    assert p.channel == "#books"
    assert p.offer_timeout == 5.0
    assert p.downloads.config == cfg.downloads
