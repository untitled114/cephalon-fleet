"""Docker health check: the bot writes a heartbeat file; `python -m amir.health` checks its age.

Exit status 0 means the heartbeat is at most `max_age` seconds old; 1 means it is stale,
missing or unreadable, or the configuration cannot be loaded. Nothing printed names a path.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from amir.config.settings import ConfigError, default_config_path, load_settings

MAX_AGE_SECONDS = 120


def write_heartbeat(path: Path, now: datetime) -> None:
    """Write `now` as ISO-8601 text atomically: a temp file in the same directory, then
    os.replace, so a reader never sees a half-written file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(now.isoformat(), encoding="utf-8")
    os.replace(tmp, path)


def check(path: Path, now: datetime, max_age: float = MAX_AGE_SECONDS) -> int:
    """0 if the heartbeat at `path` is at most `max_age` seconds from `now`, else 1."""
    try:
        beat = datetime.fromisoformat(Path(path).read_text(encoding="utf-8").strip())
        age = (now - beat).total_seconds()
    except (OSError, ValueError, TypeError):  # missing, unreadable, garbage or naive
        return 1
    return 0 if abs(age) <= max_age else 1


def main() -> int:
    try:
        settings = load_settings(default_config_path(os.environ), os.environ)
    except ConfigError:
        print("unhealthy: the configuration cannot be loaded", file=sys.stderr)
        return 1
    status = check(Path(settings.logging.heartbeat_path), datetime.now(UTC))
    print("healthy" if status == 0 else "unhealthy: no fresh heartbeat")
    return status


if __name__ == "__main__":
    raise SystemExit(main())
