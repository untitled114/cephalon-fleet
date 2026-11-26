import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from amir import health

EXAMPLE = Path(__file__).resolve().parents[1] / "config" / "amir.example.toml"
T0 = datetime(2026, 10, 26, 23, 30, tzinfo=UTC)


def test_health_fresh_stale_missing(tmp_path):
    beat = tmp_path / "data" / "heartbeat"  # the directory does not exist yet
    health.write_heartbeat(beat, T0)

    assert health.check(beat, T0 + timedelta(seconds=60)) == 0
    assert health.check(beat, T0 + timedelta(seconds=121)) == 1
    assert health.check(tmp_path / "missing", T0) == 1


def test_max_age_is_inclusive_and_configurable(tmp_path):
    beat = tmp_path / "heartbeat"
    health.write_heartbeat(beat, T0)
    assert health.check(beat, T0 + timedelta(seconds=120)) == 0
    assert health.check(beat, T0 + timedelta(seconds=31), max_age=30) == 1


def test_write_heartbeat_is_atomic_and_overwrites(tmp_path):
    beat = tmp_path / "heartbeat"
    health.write_heartbeat(beat, T0)
    later = T0 + timedelta(seconds=30)
    health.write_heartbeat(beat, later)

    assert datetime.fromisoformat(beat.read_text(encoding="utf-8")) == later
    assert sorted(p.name for p in tmp_path.iterdir()) == ["heartbeat"]  # no temp file left


def test_unreadable_heartbeat_is_unhealthy(tmp_path):
    beat = tmp_path / "heartbeat"
    beat.write_text("not a timestamp", encoding="utf-8")
    assert health.check(beat, T0) == 1
    beat.write_text(T0.replace(tzinfo=None).isoformat(), encoding="utf-8")  # naive
    assert health.check(beat, T0) == 1
    beat.unlink()
    beat.mkdir()  # a directory where the file should be
    assert health.check(beat, T0) == 1


def write_config(tmp_path: Path, beat: Path) -> Path:
    text = EXAMPLE.read_text(encoding="utf-8")
    old = 'heartbeat_path = "data/heartbeat"'
    assert old in text
    path = tmp_path / "amir.toml"
    path.write_text(text.replace(old, f'heartbeat_path = "{beat}"'), encoding="utf-8")
    return path


def test_main_checks_the_configured_heartbeat(tmp_path, monkeypatch):
    beat = tmp_path / "heartbeat"
    monkeypatch.setenv("AMIR_CONFIG", str(write_config(tmp_path, beat)))
    monkeypatch.setenv("DISCORD_TOKEN", "abc.def.ghi")

    assert health.main() == 1  # nothing written yet
    health.write_heartbeat(beat, datetime.now(UTC))
    assert health.main() == 0
    health.write_heartbeat(beat, datetime.now(UTC) - timedelta(minutes=10))
    assert health.main() == 1


def test_main_without_config_is_unhealthy(tmp_path, monkeypatch):
    monkeypatch.setenv("AMIR_CONFIG", str(tmp_path / "absent.toml"))
    monkeypatch.setenv("DISCORD_TOKEN", "abc.def.ghi")
    assert health.main() == 1


def test_python_m_amir_health_exit_code(tmp_path):
    env = {k: v for k, v in os.environ.items() if k != "DISCORD_TOKEN"}
    env["AMIR_CONFIG"] = str(tmp_path / "absent.toml")
    done = subprocess.run(
        [sys.executable, "-m", "amir.health"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 1
