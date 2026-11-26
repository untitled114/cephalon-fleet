import shutil
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from amir.storage.db import Database

FIXTURES = Path(__file__).parent / "fixtures"
CAPTURED = FIXTURES / "captured" / "2026-10-24"
VOICE_CLIP_PATH = FIXTURES / "audio" / "armstrong-small-step.ogg"  # public domain (NASA)


@pytest.fixture
def captured() -> Callable[[str], bytes]:
    def _read(name: str) -> bytes:
        return (CAPTURED / name).read_bytes()

    return _read


@pytest.fixture
def voice_clip() -> Path:
    return VOICE_CLIP_PATH


@pytest.fixture
def ffmpeg_exe() -> str:
    found = shutil.which("ffmpeg")
    if found:
        return found
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


class FakeClock:
    def __init__(self, start: datetime) -> None:
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


@pytest.fixture
def make_clock() -> Callable[[datetime], FakeClock]:
    return FakeClock


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 10, 25, 1, 21, tzinfo=UTC))


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = await Database.open(tmp_path / "amir.sqlite3")
    await database.migrate()
    yield database
    await database.close()
