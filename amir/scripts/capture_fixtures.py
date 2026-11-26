"""Capture raw source responses into tests/fixtures/captured/<date>/ (read-only HTTPS GETs).

Usage: uv run python scripts/capture_fixtures.py [--only worldstate|news|all] [--out DIR]
"""

import argparse
import sys
import tomllib
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config" / "amir.example.toml"
NY = ZoneInfo("America/New_York")
USER_AGENT = "AmirBeckett/0.1 (+Discord bot)"
TIMEOUT = 30
NEWS_NAMES = {
    "forum-pc-updates": "forum_pc_update_notes.rss.xml",
    "steam-announcements": "steam_getnewsforapp_announcements.json",
}
README_HEADER = (
    "# Captured source responses\n\n"
    "Real responses, never hand-edited. Derive edge-case fixtures from copies.\n\n"
    "| File | Source URL | HTTP | Captured (America/New_York) |\n"
    "|---|---|---|---|\n"
)


class CaptureError(Exception):
    pass


def targets(config_path: Path, only: str) -> list[tuple[str, str]]:
    config = tomllib.loads(config_path.read_text())
    world = config["worldstate"]
    worldstate = [
        ("official_worldstate.json", world["official_url"]),
        ("wfcd_pc.json", world["wfcd_url"]),
        ("wfcd_items_names.json", world["items_url"]),
    ]
    news = [(NEWS_NAMES[s["id"]], s["url"]) for s in config["news"]["sources"]]
    return {"worldstate": worldstate, "news": news, "all": worldstate + news}[only]


def folder_name(now: datetime) -> str:
    return now.astimezone(NY).strftime("%Y-%m-%d")


def check_free(folder: Path, names: list[str]) -> None:
    for name in names:
        if (folder / name).exists():
            raise CaptureError(f"{folder / name} already exists; refusing to overwrite")


def append_readme(folder: Path, name: str, url: str, status: int, when: datetime) -> None:
    readme = folder / "README.md"
    if not readme.exists():
        readme.write_text(README_HEADER)
    stamp = when.astimezone(NY).strftime("%Y-%m-%d %H:%M %Z")
    with readme.open("a") as handle:
        handle.write(f"| `{name}` | {url} | {status} | {stamp} |\n")


def save(folder: Path, name: str, url: str, status: int, body: bytes, when: datetime) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    check_free(folder, [name])
    (folder / name).write_bytes(body)
    append_readme(folder, name, url, status, when)


def fetch(url: str) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310
        return response.status, response.read()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", choices=["worldstate", "news", "all"], default="all")
    parser.add_argument("--out", type=Path, default=None)
    args = parser.parse_args(argv)
    now = datetime.now(UTC)
    folder = args.out or ROOT / "tests" / "fixtures" / "captured" / folder_name(now)
    wanted = targets(CONFIG, args.only)
    try:
        check_free(folder, [name for name, _ in wanted])
        for name, url in wanted:
            status, body = fetch(url)
            save(folder, name, url, status, body, datetime.now(UTC))
            print(f"saved {folder / name} (HTTP {status}, {len(body)} bytes)")
    except (CaptureError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
