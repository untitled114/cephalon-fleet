import importlib.util
from datetime import UTC, datetime
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[2] / "scripts" / "capture_fixtures.py"
CONFIG = Path(__file__).parents[2] / "config" / "amir.example.toml"


@pytest.fixture(scope="module")
def cap():
    spec = importlib.util.spec_from_file_location("capture_fixtures", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_targets_come_from_example_config(cap):
    worldstate = cap.targets(CONFIG, "worldstate")
    assert [name for name, _ in worldstate] == [
        "official_worldstate.json",
        "wfcd_pc.json",
        "wfcd_items_names.json",
    ]
    assert all(url.startswith("https://api.warframe") for _, url in worldstate)
    news = cap.targets(CONFIG, "news")
    assert [name for name, _ in news] == [
        "forum_pc_update_notes.rss.xml",
        "steam_getnewsforapp_announcements.json",
    ]
    assert cap.targets(CONFIG, "all") == worldstate + news


def test_folder_uses_new_york_date(cap):
    late_utc = datetime(2026, 10, 27, 2, 30, tzinfo=UTC)
    assert cap.folder_name(late_utc) == "2026-10-26"


def test_readme_created_then_appended(cap, tmp_path):
    when = datetime(2026, 10, 26, 14, 5, tzinfo=UTC)
    cap.append_readme(tmp_path, "a.json", "https://example.test/a", 200, when)
    cap.append_readme(tmp_path, "b.json", "https://example.test/b", 200, when)
    lines = (tmp_path / "README.md").read_text().splitlines()
    assert lines[0] == "# Captured source responses"
    assert lines.count("| File | Source URL | HTTP | Captured (America/New_York) |") == 1
    assert lines[-2] == "| `a.json` | https://example.test/a | 200 | 2026-10-26 10:05 EDT |"
    assert lines[-1] == "| `b.json` | https://example.test/b | 200 | 2026-10-26 10:05 EDT |"


def test_refuses_to_overwrite(cap, tmp_path):
    (tmp_path / "x.json").write_bytes(b"original")
    with pytest.raises(cap.CaptureError, match="already exists"):
        cap.check_free(tmp_path, ["x.json"])
    assert (tmp_path / "x.json").read_bytes() == b"original"
    cap.check_free(tmp_path, ["y.json"])


def test_save_writes_raw_bytes(cap, tmp_path):
    when = datetime(2026, 10, 26, 14, 5, tzinfo=UTC)
    body = b"\x00raw bytes\xff"
    cap.save(tmp_path, "z.bin", "https://example.test/z", 200, body, when)
    assert (tmp_path / "z.bin").read_bytes() == body
    assert "`z.bin`" in (tmp_path / "README.md").read_text()
