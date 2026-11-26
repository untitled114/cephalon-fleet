"""SQLite database with a versioned schema. Repos use parameterized SQL only."""

import asyncio
import sqlite3
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

_SCHEMA_V1 = """
CREATE TABLE source_state (
    source_id TEXT PRIMARY KEY,
    etag TEXT,
    last_modified TEXT,
    last_attempt_at TEXT,
    last_success_at TEXT,
    last_error TEXT,
    baseline_done INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE news_items (
    id INTEGER PRIMARY KEY,
    source_id TEXT NOT NULL,
    external_id TEXT NOT NULL,
    canonical_url TEXT,
    title TEXT,
    published_at TEXT,
    first_seen_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('baseline', 'filtered', 'queued')),
    UNIQUE (source_id, external_id)
);
CREATE INDEX idx_news_items_canonical_url ON news_items (canonical_url);

CREATE TABLE outbox (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    dedup_key TEXT NOT NULL UNIQUE,
    channel TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    status TEXT NOT NULL
        CHECK (status IN ('pending', 'sending', 'sent', 'previewed', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT,
    message_id INTEGER,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_outbox_status_next ON outbox (status, next_attempt_at);

CREATE TABLE previews (
    id INTEGER PRIMARY KEY,
    created_at TEXT NOT NULL,
    channel TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('send', 'edit')),
    payload_json TEXT NOT NULL
);

CREATE TABLE cycle_cards (
    world TEXT PRIMARY KEY,
    message_id INTEGER,
    last_state TEXT,
    last_expiry TEXT
);

CREATE TABLE baro_visits (
    activation TEXT PRIMARY KEY,
    stock_posted INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE item_names (
    unique_name TEXT PRIMARY KEY,
    name TEXT NOT NULL
);

CREATE TABLE kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""

MIGRATIONS: tuple[str, ...] = (_SCHEMA_V1,)


def to_db_time(dt: datetime) -> str:
    """Aware datetime -> ISO-8601 UTC text."""
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError("naive datetime; timestamps must be timezone-aware")
    return dt.astimezone(UTC).isoformat()


def from_db_time(s: str | None) -> datetime | None:
    """ISO-8601 text -> aware UTC datetime (None passes through)."""
    if s is None:
        return None
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        raise ValueError(f"naive timestamp in database: {s!r}")
    return dt.astimezone(UTC)


class Database:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self.conn = conn
        # One shared connection: transactions from concurrent tasks must take turns.
        self._tx_lock = asyncio.Lock()
        self._tx_task: asyncio.Task[object] | None = None

    @classmethod
    async def open(cls, path: Path) -> "Database":
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        # isolation_level=None: autocommit; transactions are explicit (BEGIN/COMMIT).
        conn = await aiosqlite.connect(path, isolation_level=None)
        conn.row_factory = aiosqlite.Row
        await conn.execute("PRAGMA journal_mode=WAL")
        await conn.execute("PRAGMA foreign_keys=ON")
        await conn.execute("PRAGMA busy_timeout=5000")
        return cls(conn)

    async def migrate(self) -> int:
        """Apply pending migrations, each atomically. Returns the new user_version."""
        async with self.conn.execute("PRAGMA user_version") as cur:
            row = await cur.fetchone()
        version = int(row[0]) if row else 0
        for index, script in enumerate(MIGRATIONS):
            if version >= index + 1:
                continue
            try:
                await self.conn.executescript(
                    f"BEGIN;\n{script}\nPRAGMA user_version = {index + 1};\nCOMMIT;"
                )
            except Exception:
                if self.conn.in_transaction:
                    await self.conn.execute("ROLLBACK")
                raise
            version = index + 1
        return version

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[aiosqlite.Connection]:
        """Atomic multi-statement write: COMMIT on success, ROLLBACK on any exception.

        Transactions from different tasks take turns on the shared connection. A nested call
        in the same task joins the open transaction: it neither begins nor commits, and the
        outermost block's COMMIT or ROLLBACK covers the nested writes. (A task spawned inside
        the block is a different task, so it waits for the lock.)

        Cancellation-safe: aiosqlite runs a statement once it is queued, even if the awaiting
        task is cancelled. So on any exception, CancelledError included, a ROLLBACK is queued
        behind whatever is pending and awaited to completion before the lock is released.
        """
        task = asyncio.current_task()
        if task is not None and self._tx_task is task:
            yield self.conn
            return
        async with self._tx_lock:
            self._tx_task = task
            try:
                await self.conn.execute("BEGIN IMMEDIATE")
                yield self.conn
                await self.conn.execute("COMMIT")
            except BaseException:
                await self._rollback()
                raise
            finally:
                self._tx_task = None

    async def _rollback(self) -> None:
        """Roll back whatever is open, finishing even if cancelled again meanwhile."""
        cancelled = False
        while True:
            try:
                await self.conn.execute("ROLLBACK")
            except asyncio.CancelledError:
                # The queued ROLLBACK still runs; queue another behind it and wait for that.
                cancelled = True
                continue
            except sqlite3.OperationalError:
                if self.conn.in_transaction:
                    raise
                # Nothing was open: BEGIN never ran, or an earlier COMMIT/ROLLBACK finished.
            break
        if cancelled:
            raise asyncio.CancelledError

    async def close(self) -> None:
        await self.conn.close()
