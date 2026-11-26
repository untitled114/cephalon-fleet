import json
from datetime import timedelta
from pathlib import Path

from amir.storage.db import Database, to_db_time
from amir.storage.repos import VOICE_HOLD_KEY, VoiceHold, VoiceHoldRepo


async def stored(db: Database) -> str | None:
    async with db.conn.execute("SELECT value FROM kv WHERE key = ?", (VOICE_HOLD_KEY,)) as cur:
        row = await cur.fetchone()
    return None if row is None else row["value"]


async def test_no_hold_until_one_is_saved(db: Database):
    holds = VoiceHoldRepo(db)

    assert await holds.get() is None
    assert await stored(db) is None


async def test_save_records_when_and_why_only(db: Database, clock):
    holds = VoiceHoldRepo(db)
    hold = VoiceHold(since=clock.now(), reason="kicked")

    await holds.save(hold)

    assert await holds.get() == hold
    raw = await stored(db)
    assert raw is not None
    # The time it was set (ISO-8601 UTC) and the reason: no member data.
    assert json.loads(raw) == {"since": to_db_time(clock.now()), "reason": "kicked"}


async def test_a_later_hold_replaces_the_earlier_one(db: Database, clock):
    holds = VoiceHoldRepo(db)
    await holds.save(VoiceHold(clock.now(), "kicked"))
    clock.advance(90)

    await holds.save(VoiceHold(clock.now(), "moved"))

    assert await holds.get() == VoiceHold(since=clock.now(), reason="moved")


async def test_clear_reports_whether_there_was_a_hold(db: Database, clock):
    holds = VoiceHoldRepo(db)
    assert await holds.clear() is False

    await holds.save(VoiceHold(clock.now(), "moved"))

    assert await holds.clear() is True
    assert await holds.get() is None
    assert await holds.clear() is False


async def test_hold_survives_a_restart(tmp_path: Path, clock):
    path = tmp_path / "amir.sqlite3"
    first = await Database.open(path)
    await first.migrate()
    hold = VoiceHold(clock.now(), "kicked")
    await VoiceHoldRepo(first).save(hold)
    await first.close()
    clock.advance(timedelta(hours=5).total_seconds())

    reopened = await Database.open(path)
    try:
        await reopened.migrate()
        assert await VoiceHoldRepo(reopened).get() == hold
    finally:
        await reopened.close()
