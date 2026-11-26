import asyncio
import sqlite3
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from amir.clock import SystemClock
from amir.storage.db import Database, from_db_time, to_db_time

EXPECTED_TABLES = {
    "source_state",
    "news_items",
    "outbox",
    "previews",
    "cycle_cards",
    "baro_visits",
    "item_names",
    "kv",
}


async def test_migrate_is_idempotent(tmp_path: Path):
    database = await Database.open(tmp_path / "a.sqlite3")
    try:
        assert await database.migrate() == 1
        assert await database.migrate() == 1
    finally:
        await database.close()


async def test_data_survives_reopen(tmp_path: Path):
    path = tmp_path / "a.sqlite3"
    database = await Database.open(path)
    await database.migrate()
    await database.conn.execute("INSERT INTO kv (key, value) VALUES (?, ?)", ("k", "v"))
    await database.close()

    reopened = await Database.open(path)
    try:
        assert await reopened.migrate() == 1
        async with reopened.conn.execute("SELECT value FROM kv WHERE key = ?", ("k",)) as cur:
            row = await cur.fetchone()
        assert row["value"] == "v"
    finally:
        await reopened.close()


async def test_wal_enabled(db: Database):
    async with db.conn.execute("PRAGMA journal_mode") as cur:
        assert (await cur.fetchone())[0] == "wal"


async def test_pragmas_and_tables(db: Database):
    async with db.conn.execute("PRAGMA foreign_keys") as cur:
        assert (await cur.fetchone())[0] == 1
    async with db.conn.execute("PRAGMA busy_timeout") as cur:
        assert (await cur.fetchone())[0] == 5000
    async with db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'") as cur:
        names = {r["name"] for r in await cur.fetchall()}
    assert EXPECTED_TABLES <= names


async def test_open_creates_missing_parent_dir(tmp_path: Path):
    database = await Database.open(tmp_path / "data" / "nested" / "amir.sqlite3")
    await database.close()
    assert (tmp_path / "data" / "nested" / "amir.sqlite3").exists()


async def test_check_constraints_reject_bad_status(db: Database):
    with pytest.raises(Exception, match="CHECK"):
        await db.conn.execute(
            "INSERT INTO outbox (kind, dedup_key, channel, payload_json, status, created_at,"
            " updated_at) VALUES ('k', 'd', 'c', '{}', 'bogus', 't', 't')"
        )


async def test_transaction_commits_and_rolls_back(db: Database):
    async with db.transaction() as conn:
        await conn.execute("INSERT INTO kv (key, value) VALUES (?, ?)", ("a", "1"))
    with pytest.raises(RuntimeError):
        async with db.transaction() as conn:
            await conn.execute("INSERT INTO kv (key, value) VALUES (?, ?)", ("b", "2"))
            raise RuntimeError("boom")
    async with db.conn.execute("SELECT key FROM kv") as cur:
        assert [r["key"] for r in await cur.fetchall()] == ["a"]


async def test_concurrent_transactions_serialise(db: Database):
    order: list[str] = []

    async def write(key: str) -> None:
        async with db.transaction() as conn:
            order.append(f"{key} begin")
            await conn.execute("INSERT INTO kv (key, value) VALUES (?, ?)", (key, key))
            await asyncio.sleep(0)  # yield to the other task while the transaction is open
            order.append(f"{key} end")

    await asyncio.gather(write("a"), write("b"))

    assert order == ["a begin", "a end", "b begin", "b end"]
    async with db.conn.execute("SELECT key FROM kv ORDER BY key") as cur:
        assert [r["key"] for r in await cur.fetchall()] == ["a", "b"]


async def test_rolled_back_transaction_does_not_swallow_a_waiting_one(db: Database):
    async def fail() -> None:
        async with db.transaction() as conn:
            await conn.execute("INSERT INTO kv (key, value) VALUES (?, ?)", ("lost", "1"))
            await asyncio.sleep(0)
            raise RuntimeError("boom")

    async def succeed() -> None:
        async with db.transaction() as conn:
            await conn.execute("INSERT INTO kv (key, value) VALUES (?, ?)", ("kept", "1"))

    results = await asyncio.gather(fail(), succeed(), return_exceptions=True)

    assert isinstance(results[0], RuntimeError)
    assert results[1] is None
    async with db.conn.execute("SELECT key FROM kv") as cur:
        assert [r["key"] for r in await cur.fetchall()] == ["kept"]


async def test_nested_transaction_joins_the_outer_one(db: Database):
    async with db.transaction() as outer:
        await outer.execute("INSERT INTO kv (key, value) VALUES (?, ?)", ("a", "1"))
        async with db.transaction() as inner:
            assert inner is outer
            await inner.execute("INSERT INTO kv (key, value) VALUES (?, ?)", ("b", "1"))
        assert db.conn.in_transaction  # the nested exit committed nothing
    assert not db.conn.in_transaction
    async with db.conn.execute("SELECT key FROM kv ORDER BY key") as cur:
        assert [r["key"] for r in await cur.fetchall()] == ["a", "b"]


class Boom(Exception):
    pass


async def test_outer_rollback_undoes_nested_writes(db: Database):
    with pytest.raises(Boom):
        async with db.transaction() as outer:
            await outer.execute("INSERT INTO kv (key, value) VALUES (?, ?)", ("a", "1"))
            async with db.transaction() as inner:
                await inner.execute("INSERT INTO kv (key, value) VALUES (?, ?)", ("b", "1"))
            raise Boom
    assert not db.conn.in_transaction
    async with db.conn.execute("SELECT COUNT(*) FROM kv") as cur:
        assert (await cur.fetchone())[0] == 0


async def test_reentrancy_is_per_task(db: Database):
    order: list[str] = []

    async def write(key: str) -> None:
        async with db.transaction():
            order.append(f"{key} begin")
            async with db.transaction() as inner:
                await inner.execute("INSERT INTO kv (key, value) VALUES (?, ?)", (key, key))
                await asyncio.sleep(0)  # the other task must not get in here
            await asyncio.sleep(0)
            order.append(f"{key} end")

    await asyncio.gather(write("a"), write("b"))

    assert order == ["a begin", "a end", "b begin", "b end"]


async def assert_connection_usable(db: Database) -> None:
    assert not db.conn.in_transaction
    async with db.transaction() as conn:
        await conn.execute("INSERT INTO kv (key, value) VALUES (?, ?)", ("after", "1"))
    async with db.conn.execute("SELECT key FROM kv WHERE key = 'after'") as cur:
        assert await cur.fetchone() is not None


async def test_cancel_during_begin_leaves_connection_usable(db: Database):
    async def write() -> None:
        async with db.transaction() as conn:
            await conn.execute("INSERT INTO kv (key, value) VALUES (?, ?)", ("lost", "1"))

    task = asyncio.create_task(write())
    await asyncio.sleep(0)  # the task is now awaiting its BEGIN IMMEDIATE
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await assert_connection_usable(db)
    async with db.conn.execute("SELECT key FROM kv WHERE key = 'lost'") as cur:
        assert await cur.fetchone() is None


async def test_cancel_inside_transaction_rolls_back(db: Database):
    inside = asyncio.Event()

    async def write() -> None:
        async with db.transaction() as conn:
            await conn.execute("INSERT INTO kv (key, value) VALUES (?, ?)", ("lost", "1"))
            inside.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(write())
    await inside.wait()
    task.cancel()
    await asyncio.sleep(0)  # let the rollback start, then cancel it too
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await assert_connection_usable(db)
    async with db.conn.execute("SELECT key FROM kv WHERE key = 'lost'") as cur:
        assert await cur.fetchone() is None


async def test_cancel_during_commit_leaves_connection_usable(db: Database):
    body_done = asyncio.Event()

    async def write() -> None:
        async with db.transaction() as conn:
            await conn.execute("INSERT INTO kv (key, value) VALUES (?, ?)", ("k", "1"))
            body_done.set()  # the context manager now awaits COMMIT

    task = asyncio.create_task(write())
    await body_done.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    await assert_connection_usable(db)


async def test_failed_rollback_is_raised_not_hidden(db: Database, monkeypatch: pytest.MonkeyPatch):
    real_execute = db.conn.execute

    async def execute(sql: str, *args: object):
        if sql == "ROLLBACK":
            raise sqlite3.OperationalError("disk I/O error")
        return await real_execute(sql, *args)

    monkeypatch.setattr(db.conn, "execute", execute)
    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        async with db.transaction():
            raise Boom
    monkeypatch.undo()
    await db.conn.execute("ROLLBACK")  # clean up for the fixture's close


async def test_failed_migration_rolls_back(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    from amir.storage import db as dbmod

    monkeypatch.setattr(dbmod, "MIGRATIONS", ("CREATE TABLE t (x); CREATE TABLE t (x);",))
    database = await Database.open(tmp_path / "a.sqlite3")
    try:
        with pytest.raises(Exception, match="already exists"):
            await database.migrate()
        async with database.conn.execute("PRAGMA user_version") as cur:
            assert (await cur.fetchone())[0] == 0
        async with database.conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='t'"
        ) as cur:
            assert (await cur.fetchone())[0] == 0
    finally:
        await database.close()


def test_time_helpers_round_trip_and_reject_naive():
    est = timezone(timedelta(hours=-5))
    stamp = to_db_time(datetime(2026, 10, 24, 20, 21, tzinfo=est))
    assert stamp == "2026-10-25T01:21:00+00:00"
    assert from_db_time(stamp) == datetime(2026, 10, 25, 1, 21, tzinfo=UTC)
    assert from_db_time(None) is None
    with pytest.raises(ValueError):
        to_db_time(datetime(2026, 10, 24, 20, 21))
    with pytest.raises(ValueError):
        from_db_time("2026-10-24T20:21:00")


def test_clocks(clock):
    assert SystemClock().now().tzinfo is UTC
    start = clock.now()
    clock.advance(90)
    assert clock.now() - start == timedelta(seconds=90)
