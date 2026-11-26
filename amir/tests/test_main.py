import asyncio
import logging
import os
import signal
import subprocess
import sys
from pathlib import Path

import discord
import pytest

import amir.__main__ as amir_main
from amir.__main__ import main
from amir.bot.client import AmirBot
from amir.bot.commands import register_amir_commands

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "amir.example.toml"
TOKEN = "abc.def.ghi"
RUNTIME_IDS = (
    "discord.guild_id",
    "discord.voice_channel_id",
    "discord.news_channel_id",
    "discord.cycles_channel_id",
    "discord.voice_log_channel_id",
)


@pytest.fixture
def restore_logging():
    """Run mode installs Amir's JSON handler on the root logger; put the original back."""
    root = logging.getLogger()
    library = logging.getLogger("discord")
    handlers, level, library_level = list(root.handlers), root.level, library.level
    yield
    root.handlers[:] = handlers
    root.setLevel(level)
    library.setLevel(library_level)


def config_variant(tmp_path: Path, *replacements: tuple[str, str]) -> Path:
    text = EXAMPLE.read_text(encoding="utf-8")
    for old, new in replacements:
        assert old in text
        text = text.replace(old, new, 1)
    path = tmp_path / "amir.toml"
    path.write_text(text, encoding="utf-8")
    return path


def runnable_config(tmp_path: Path) -> Path:
    """The example config with every runtime ID set and every path under tmp_path."""
    ids = [
        ("guild_id = 0", "guild_id = 2001"),
        ("voice_channel_id = 0", "voice_channel_id = 3001"),
        ("news_channel_id = 0", "news_channel_id = 3002"),
        ("cycles_channel_id = 0", "cycles_channel_id = 3003"),
        ("voice_log_channel_id = 0", "voice_log_channel_id = 3004"),
    ]
    paths = [
        ('db_path = "data/amir.sqlite3"', f'db_path = "{tmp_path / "amir.sqlite3"}"'),
        ('heartbeat_path = "data/heartbeat"', f'heartbeat_path = "{tmp_path / "heartbeat"}"'),
        ('greetings_dir = "assets/greetings"', f'greetings_dir = "{tmp_path / "greetings"}"'),
    ]
    return config_variant(tmp_path, *ids, *paths)


def test_check_config_ok(monkeypatch, capsys):
    monkeypatch.setenv("AMIR_CONFIG", str(EXAMPLE))
    monkeypatch.setenv("DISCORD_TOKEN", TOKEN)

    assert main(["--check-config"]) == 0

    out, err = capsys.readouterr()
    assert "Publishing: OFF" in out
    assert TOKEN not in out + err
    for name in RUNTIME_IDS:  # the example leaves every runtime ID unset
        assert name in out
    assert "forum-pc-updates" in out and "steam-announcements" in out
    for path_hint in ("data/", "assets/", "amir.example.toml", str(EXAMPLE.parent)):
        assert path_hint not in out


def test_check_config_missing_token(monkeypatch, capsys):
    monkeypatch.setenv("AMIR_CONFIG", str(EXAMPLE))
    monkeypatch.delenv("DISCORD_TOKEN", raising=False)

    assert main(["--check-config"]) == 2

    _, err = capsys.readouterr()
    assert "DISCORD_TOKEN" in err


def test_check_config_missing_file_names_only_the_file(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("AMIR_CONFIG", str(tmp_path / "amir.toml"))
    monkeypatch.setenv("DISCORD_TOKEN", TOKEN)

    assert main(["--check-config"]) == 2

    out, err = capsys.readouterr()
    assert "amir.toml" in err
    assert str(tmp_path) not in out + err


def test_check_config_reports_publishing_on_and_no_missing_ids(tmp_path, monkeypatch, capsys):
    path = config_variant(
        tmp_path,
        ("enabled = false            # HARD RULE", "enabled = true            # HARD RULE"),
    )
    monkeypatch.setenv("AMIR_CONFIG", str(path))
    monkeypatch.setenv("DISCORD_TOKEN", TOKEN)
    assert main(["--check-config"]) == 0
    assert "Publishing: ON" in capsys.readouterr().out

    monkeypatch.setenv("AMIR_CONFIG", str(runnable_config(tmp_path)))
    assert main(["--check-config"]) == 0
    out = capsys.readouterr().out
    assert not [name for name in RUNTIME_IDS if name in out]
    assert str(tmp_path) not in out


def test_run_refuses_missing_runtime_ids(monkeypatch, capsys):
    monkeypatch.setenv("AMIR_CONFIG", str(EXAMPLE))
    monkeypatch.setenv("DISCORD_TOKEN", TOKEN)

    async def never(self, token, **kwargs):
        raise AssertionError("the bot must not start without its Discord IDs")

    monkeypatch.setattr(AmirBot, "start", never)

    assert main([]) == 2

    out, err = capsys.readouterr()
    for name in RUNTIME_IDS:
        assert name in err
    assert TOKEN not in out + err


async def query_db(db) -> None:
    async with db.conn.execute("SELECT 1"):
        pass


async def query(bot: AmirBot) -> None:
    await query_db(bot.services.db)


class SignalRecorder:
    """Replaces loop.add_signal_handler: records the handlers instead of installing them.

    Set on the loop class, an instance is not bound, so it is called without the loop.
    """

    def __init__(self) -> None:
        self.handlers: dict[int, tuple] = {}

    def __call__(self, sig, callback, *args) -> None:
        self.handlers[sig] = (callback, args)


def test_run_mode_wires_signals_and_shuts_down(tmp_path, monkeypatch, capsys, restore_logging):
    monkeypatch.setenv("AMIR_CONFIG", str(runnable_config(tmp_path)))
    monkeypatch.setenv("DISCORD_TOKEN", TOKEN)
    recorder = SignalRecorder()
    monkeypatch.setattr(asyncio.SelectorEventLoop, "add_signal_handler", recorder)
    seen: dict[str, object] = {}
    gateway_closed = asyncio.Event()
    real_client_close = discord.Client.close

    async def client_close(self) -> None:
        await real_client_close(self)
        gateway_closed.set()

    async def fake_start(self, token, **kwargs):  # stands in for the gateway connection
        seen["bot"], seen["token"] = self, token
        callback, args = recorder.handlers[signal.SIGTERM]
        callback(*args)  # what the loop does when SIGTERM arrives
        await gateway_closed.wait()  # the real start returns once the gateway is closed

    monkeypatch.setattr(discord.Client, "close", client_close)
    monkeypatch.setattr(AmirBot, "start", fake_start)

    assert main([]) == 0

    assert set(recorder.handlers) == {signal.SIGTERM, signal.SIGINT}
    bot = seen["bot"]
    assert isinstance(bot, AmirBot)
    assert seen["token"] == TOKEN
    assert bot._register_commands is register_amir_commands  # the /amir commands
    assert bot.is_closed()
    assert bot.services.http.closed
    with pytest.raises(ValueError, match="no active connection"):
        asyncio.run(query(bot))  # the database was closed
    out, err = capsys.readouterr()
    assert TOKEN not in out + err


@pytest.mark.parametrize(
    ("error", "logged"),
    [
        (discord.LoginFailure("Improper token has been passed."), "Discord rejected the token"),
        (RuntimeError("boom"), "RuntimeError: boom"),
    ],
)
def test_run_mode_failure_exits_1_after_cleanup(
    error, logged, tmp_path, monkeypatch, capsys, restore_logging
):
    monkeypatch.setenv("AMIR_CONFIG", str(runnable_config(tmp_path)))
    monkeypatch.setenv("DISCORD_TOKEN", TOKEN)
    monkeypatch.setattr(asyncio.SelectorEventLoop, "add_signal_handler", SignalRecorder())
    seen: dict[str, AmirBot] = {}

    async def failing_start(self, token, **kwargs):
        seen["bot"] = self
        raise error

    monkeypatch.setattr(AmirBot, "start", failing_start)

    assert main([]) == 1

    assert seen["bot"].is_closed()
    assert seen["bot"].services.http.closed
    out, err = capsys.readouterr()
    assert logged in out
    assert TOKEN not in out + err


def test_run_mode_database_failure_exits_1(tmp_path, monkeypatch, capsys, restore_logging):
    config = runnable_config(tmp_path)
    blocker = tmp_path / "not-a-directory"
    blocker.write_text("", encoding="utf-8")
    text = config.read_text(encoding="utf-8")
    old = f'db_path = "{tmp_path / "amir.sqlite3"}"'
    assert old in text
    config.write_text(
        text.replace(old, f'db_path = "{blocker / "amir.sqlite3"}"'), encoding="utf-8"
    )
    monkeypatch.setenv("AMIR_CONFIG", str(config))
    monkeypatch.setenv("DISCORD_TOKEN", TOKEN)

    async def never(self, token, **kwargs):
        raise AssertionError("the bot must not start without its database")

    monkeypatch.setattr(AmirBot, "start", never)

    assert main([]) == 1

    out, err = capsys.readouterr()
    assert "the database could not be opened or migrated" in out
    assert TOKEN not in out + err


def test_run_mode_build_failure_exits_1_after_cleanup(
    tmp_path, monkeypatch, capsys, restore_logging
):
    monkeypatch.setenv("AMIR_CONFIG", str(runnable_config(tmp_path)))
    monkeypatch.setenv("DISCORD_TOKEN", TOKEN)
    opened: dict[str, object] = {}

    def broken_build(client, settings, db, http, fetcher):
        opened["db"], opened["http"] = db, http
        raise RuntimeError("wiring failed")

    async def never(self, token, **kwargs):
        raise AssertionError("a bot that could not be built must not start")

    monkeypatch.setattr(amir_main, "build_services", broken_build)
    monkeypatch.setattr(AmirBot, "start", never)

    assert main([]) == 1

    assert opened["http"].closed
    with pytest.raises(ValueError, match="no active connection"):
        asyncio.run(query_db(opened["db"]))
    out, err = capsys.readouterr()
    assert "the bot could not be set up" in out
    assert "RuntimeError: wiring failed" in out
    assert TOKEN not in out + err


def test_python_m_amir_check_config(tmp_path):
    env = dict(os.environ, AMIR_CONFIG=str(EXAMPLE), DISCORD_TOKEN=TOKEN)
    done = subprocess.run(
        [sys.executable, "-m", "amir", "--check-config"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 0
    assert "Publishing: OFF" in done.stdout
    assert TOKEN not in done.stdout + done.stderr
