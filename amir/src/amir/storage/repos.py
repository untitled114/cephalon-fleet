"""Repositories over the SQLite schema. Parameterized SQL only."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal, cast, get_args

import aiosqlite

from amir.clock import Clock, SystemClock
from amir.news.model import NewsItem
from amir.publishing.gate import ChannelKey
from amir.storage.db import Database, from_db_time, to_db_time

PreviewAction = Literal["send", "edit"]
OutboxStatus = Literal["pending", "sending", "sent", "previewed", "failed"]

# Per channel: while gated every cycle flip is a preview, and those must never push out the
# news and Baro previews the owner has to approve.
MAX_PREVIEWS = 500


class PreviewRepo:
    """What Amir would have posted while publishing is disabled; keeps the newest 500 rows of
    each channel."""

    def __init__(self, db: Database, clock: Clock) -> None:
        self._db = db
        self._clock = clock

    async def add(self, channel: str, action: PreviewAction, payload: dict[str, Any]) -> None:
        payload_json = json.dumps(payload, ensure_ascii=False)
        created_at = to_db_time(self._clock.now())
        async with self._db.transaction() as conn:
            await conn.execute(
                "INSERT INTO previews (created_at, channel, action, payload_json)"
                " VALUES (?, ?, ?, ?)",
                (created_at, str(channel), action, payload_json),
            )
            await conn.execute(
                "DELETE FROM previews WHERE channel = ? AND id NOT IN"
                " (SELECT id FROM previews WHERE channel = ? ORDER BY id DESC LIMIT ?)",
                (str(channel), str(channel), MAX_PREVIEWS),
            )

    async def count(self) -> int:
        async with self._db.conn.execute("SELECT COUNT(*) FROM previews") as cur:
            row = await cur.fetchone()
        return int(row[0]) if row else 0

    async def recent(self, limit: int) -> list[dict[str, Any]]:
        """Newest first: id, created_at (aware UTC), channel, action, payload (decoded)."""
        async with self._db.conn.execute(
            "SELECT id, created_at, channel, action, payload_json FROM previews"
            " ORDER BY id DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
        return [
            {
                "id": row["id"],
                "created_at": from_db_time(row["created_at"]),
                "channel": row["channel"],
                "action": row["action"],
                "payload": json.loads(row["payload_json"]),
            }
            for row in rows
        ]


_SELECT_OUTBOX = (
    "SELECT id, kind, dedup_key, channel, payload_json, status, attempts, next_attempt_at,"
    " message_id, last_error, created_at, updated_at FROM outbox"
)


@dataclass(frozen=True)
class OutboxRow:
    """One outbox row; `payload` is the decoded {"cards", "text", "role_ping_id"} dict."""

    id: int
    kind: str
    dedup_key: str
    channel: ChannelKey
    payload: dict[str, Any]
    status: OutboxStatus
    attempts: int
    next_attempt_at: datetime | None
    message_id: int | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime


def _outbox_row(row: aiosqlite.Row) -> OutboxRow:
    return OutboxRow(
        id=row["id"],
        kind=row["kind"],
        dedup_key=row["dedup_key"],
        channel=ChannelKey(row["channel"]),
        payload=json.loads(row["payload_json"]),
        status=row["status"],
        attempts=row["attempts"],
        next_attempt_at=from_db_time(row["next_attempt_at"]),
        message_id=row["message_id"],
        last_error=row["last_error"],
        created_at=cast(datetime, from_db_time(row["created_at"])),
        updated_at=cast(datetime, from_db_time(row["updated_at"])),
    )


class OutboxRepo:
    """Durable queue of channel posts, one row per dedup key.

    Lifecycle: pending -> sending -> sent | previewed | failed, or back to pending for a retry;
    while publishing is off, pending -> previewed directly. Every write is its own short
    transaction (joining the caller's, if one is open), so none spans a Discord call.
    """

    def __init__(self, db: Database, clock: Clock | None = None) -> None:
        self._db = db
        self._clock = clock or SystemClock()

    async def enqueue(
        self, kind: str, dedup_key: str, channel: ChannelKey, payload: dict[str, Any]
    ) -> bool:
        """Queue a post. Returns False, changing nothing, when `dedup_key` already exists."""
        channel_key = ChannelKey(channel)
        if channel_key is ChannelKey.VOICE_LOG:
            raise ValueError("voice-log lines are never written to SQLite")
        payload_json = json.dumps(payload, ensure_ascii=False)
        now = self._now()
        return await self._write(
            "INSERT INTO outbox (kind, dedup_key, channel, payload_json, status, created_at,"
            " updated_at) VALUES (?, ?, ?, ?, 'pending', ?, ?) ON CONFLICT (dedup_key) DO NOTHING",
            (kind, dedup_key, channel_key.value, payload_json, now, now),
        )

    async def due(self, now: datetime, limit: int = 20) -> list[OutboxRow]:
        """Pending rows whose next attempt is not in the future, oldest first."""
        # to_db_time always writes UTC isoformat ("...+00:00"), so text order is time order.
        return await self._select(
            _SELECT_OUTBOX + " WHERE status = 'pending'"
            " AND (next_attempt_at IS NULL OR next_attempt_at <= ?) ORDER BY id LIMIT ?",
            (to_db_time(now), limit),
        )

    async def get_by_key(self, key: str) -> OutboxRow | None:
        rows = await self._select(_SELECT_OUTBOX + " WHERE dedup_key = ?", (key,))
        return rows[0] if rows else None

    async def counts_by_status(self) -> dict[OutboxStatus, int]:
        """How many rows are in each status: every status, zeros included, in lifecycle order."""
        async with self._db.conn.execute(
            "SELECT status, COUNT(*) FROM outbox GROUP BY status"
        ) as cur:
            rows = await cur.fetchall()
        counts: dict[OutboxStatus, int] = dict.fromkeys(get_args(OutboxStatus), 0)
        counts.update({row[0]: int(row[1]) for row in rows})
        return counts

    async def inflight(self) -> list[OutboxRow]:
        """Rows left 'sending', e.g. by a crash during the Discord call."""
        return await self._select(_SELECT_OUTBOX + " WHERE status = 'sending' ORDER BY id", ())

    async def mark_sending(self, row_id: int) -> bool:
        """Claim a pending row and count the attempt. False if it is no longer pending."""
        return await self._write(
            "UPDATE outbox SET status = 'sending', attempts = attempts + 1, updated_at = ?"
            " WHERE id = ? AND status = 'pending'",
            (self._now(), row_id),
        )

    async def claim_previewed(self, row_id: int) -> bool:
        """Publishing off: settle a pending row as previewed in one write, counting the attempt.

        Made before the preview is recorded, so the row never sits in 'sending' where a restart
        with publishing on could replay it. False if the row is no longer pending.
        """
        return await self._write(
            "UPDATE outbox SET status = 'previewed', attempts = attempts + 1, updated_at = ?"
            " WHERE id = ? AND status = 'pending'",
            (self._now(), row_id),
        )

    async def mark_sent(self, row_id: int, message_id: int | None) -> None:
        await self._write(
            "UPDATE outbox SET status = 'sent', message_id = ?, updated_at = ? WHERE id = ?",
            (message_id, self._now(), row_id),
        )

    async def mark_previewed(self, row_id: int) -> None:
        await self._write(
            "UPDATE outbox SET status = 'previewed', updated_at = ? WHERE id = ?",
            (self._now(), row_id),
        )

    async def mark_retry(self, row_id: int, error: str, next_at: datetime) -> None:
        await self._write(
            "UPDATE outbox SET status = 'pending', last_error = ?, next_attempt_at = ?,"
            " updated_at = ? WHERE id = ?",
            (error, to_db_time(next_at), self._now(), row_id),
        )

    async def mark_failed(self, row_id: int, error: str) -> None:
        await self._write(
            "UPDATE outbox SET status = 'failed', last_error = ?, updated_at = ? WHERE id = ?",
            (error, self._now(), row_id),
        )

    def _now(self) -> str:
        return to_db_time(self._clock.now())

    async def _select(self, sql: str, params: tuple[Any, ...]) -> list[OutboxRow]:
        async with self._db.conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [_outbox_row(row) for row in rows]

    async def _write(self, sql: str, params: tuple[Any, ...]) -> bool:
        """One statement in its own short transaction; True if it changed exactly one row."""
        async with self._db.transaction() as conn:
            cur = await conn.execute(sql, params)
            return cur.rowcount == 1


@dataclass(frozen=True)
class SourceState:
    """Polling state of one news source; the defaults describe a source never polled."""

    source_id: str
    etag: str | None = None
    last_modified: str | None = None
    last_attempt_at: datetime | None = None
    last_success_at: datetime | None = None
    last_error: str | None = None
    baseline_done: bool = False


class SourceStateRepo:
    """Per-source conditional-request validators, poll outcome and baseline flag.

    Each write is an upsert in its own short transaction, joining the caller's if one is open.
    """

    def __init__(self, db: Database, clock: Clock | None = None) -> None:
        self._db = db
        self._clock = clock or SystemClock()

    async def get(self, source_id: str) -> SourceState:
        async with self._db.conn.execute(
            "SELECT etag, last_modified, last_attempt_at, last_success_at, last_error,"
            " baseline_done FROM source_state WHERE source_id = ?",
            (source_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return SourceState(source_id)
        return SourceState(
            source_id=source_id,
            etag=row["etag"],
            last_modified=row["last_modified"],
            last_attempt_at=from_db_time(row["last_attempt_at"]),
            last_success_at=from_db_time(row["last_success_at"]),
            last_error=row["last_error"],
            baseline_done=bool(row["baseline_done"]),
        )

    async def record_attempt(self, source_id: str) -> None:
        await self._write(
            "INSERT INTO source_state (source_id, last_attempt_at) VALUES (?, ?)"
            " ON CONFLICT (source_id) DO UPDATE SET last_attempt_at = excluded.last_attempt_at",
            (source_id, self._now()),
        )

    async def record_success(
        self, source_id: str, etag: str | None, last_modified: str | None
    ) -> None:
        """Store the validators to send next time and clear any recorded error."""
        await self._write(
            "INSERT INTO source_state (source_id, etag, last_modified, last_success_at,"
            " last_error) VALUES (?, ?, ?, ?, NULL) ON CONFLICT (source_id) DO UPDATE SET"
            " etag = excluded.etag, last_modified = excluded.last_modified,"
            " last_success_at = excluded.last_success_at, last_error = NULL",
            (source_id, etag, last_modified, self._now()),
        )

    async def record_error(self, source_id: str, msg: str) -> None:
        await self._write(
            "INSERT INTO source_state (source_id, last_error) VALUES (?, ?)"
            " ON CONFLICT (source_id) DO UPDATE SET last_error = excluded.last_error",
            (source_id, msg),
        )

    async def mark_baseline_done(self, source_id: str) -> None:
        await self._write(
            "INSERT INTO source_state (source_id, baseline_done) VALUES (?, 1)"
            " ON CONFLICT (source_id) DO UPDATE SET baseline_done = 1",
            (source_id,),
        )

    def _now(self) -> str:
        return to_db_time(self._clock.now())

    async def _write(self, sql: str, params: tuple[Any, ...]) -> None:
        async with self._db.transaction() as conn:
            await conn.execute(sql, params)


NewsItemStatus = Literal["baseline", "filtered", "queued"]


class NewsItemRepo:
    """Every news item ever seen, per source: the durable dedup record."""

    def __init__(self, db: Database, clock: Clock | None = None) -> None:
        self._db = db
        self._clock = clock or SystemClock()

    @property
    def db(self) -> Database:
        """The database, so a caller can wrap several repos' writes in one transaction."""
        return self._db

    async def exists(self, source_id: str, external_id: str) -> bool:
        return await self._any(
            "SELECT 1 FROM news_items WHERE source_id = ? AND external_id = ? LIMIT 1",
            (source_id, external_id),
        )

    async def url_seen(self, canonical_url: str) -> bool:
        """True if any source already has an item at this URL. An empty URL is never seen."""
        if not canonical_url:
            return False
        return await self._any(
            "SELECT 1 FROM news_items WHERE canonical_url = ? LIMIT 1", (canonical_url,)
        )

    async def add(self, item: NewsItem, status: NewsItemStatus) -> None:
        published_at = None if item.published_at is None else to_db_time(item.published_at)
        async with self._db.transaction() as conn:
            await conn.execute(
                "INSERT INTO news_items (source_id, external_id, canonical_url, title,"
                " published_at, first_seen_at, status) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    item.source_id,
                    item.external_id,
                    item.canonical_url or None,
                    item.title,
                    published_at,
                    to_db_time(self._clock.now()),
                    status,
                ),
            )

    async def _any(self, sql: str, params: tuple[Any, ...]) -> bool:
        async with self._db.conn.execute(sql, params) as cur:
            return await cur.fetchone() is not None


class BaroVisitRepo:
    """Per-visit flags, keyed by the visit's activation; today, whether late stock was posted."""

    def __init__(self, db: Database) -> None:
        self._db = db

    @property
    def db(self) -> Database:
        """The database, so a caller can wrap several repos' writes in one transaction."""
        return self._db

    async def stock_posted(self, activation: datetime) -> bool:
        async with self._db.conn.execute(
            "SELECT stock_posted FROM baro_visits WHERE activation = ?",
            (to_db_time(activation),),
        ) as cur:
            row = await cur.fetchone()
        return bool(row and row["stock_posted"])

    async def mark_stock_posted(self, activation: datetime) -> None:
        async with self._db.transaction() as conn:
            await conn.execute(
                "INSERT INTO baro_visits (activation, stock_posted) VALUES (?, 1)"
                " ON CONFLICT (activation) DO UPDATE SET stock_posted = 1",
                (to_db_time(activation),),
            )


class CycleCardRepo:
    """Per world: the living card's message id and the state and expiry it last showed."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self, world: str) -> tuple[int | None, str | None, datetime | None]:
        """(message_id, last_state, last_expiry); all None for a world never shown."""
        async with self._db.conn.execute(
            "SELECT message_id, last_state, last_expiry FROM cycle_cards WHERE world = ?",
            (world,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None, None, None
        return row["message_id"], row["last_state"], from_db_time(row["last_expiry"])

    async def save(self, world: str, message_id: int, state: str, expiry: datetime) -> None:
        """A card was posted: remember its message id with what it shows."""
        await self._write(
            "INSERT INTO cycle_cards (world, message_id, last_state, last_expiry)"
            " VALUES (?, ?, ?, ?) ON CONFLICT (world) DO UPDATE SET"
            " message_id = excluded.message_id, last_state = excluded.last_state,"
            " last_expiry = excluded.last_expiry",
            (world, message_id, state, to_db_time(expiry)),
        )

    async def save_state(self, world: str, state: str, expiry: datetime) -> None:
        """Record what the card shows (or would show), keeping any stored message id."""
        await self._write(
            "INSERT INTO cycle_cards (world, last_state, last_expiry) VALUES (?, ?, ?)"
            " ON CONFLICT (world) DO UPDATE SET last_state = excluded.last_state,"
            " last_expiry = excluded.last_expiry",
            (world, state, to_db_time(expiry)),
        )

    async def _write(self, sql: str, params: tuple[Any, ...]) -> None:
        async with self._db.transaction() as conn:
            await conn.execute(sql, params)


ITEM_NAMES_FETCHED_AT_KEY = "item_names_fetched_at"


class ItemNameRepo:
    """WFCD's item database (uniqueName -> name), cached so Baro's stock resolves while WFCD is
    down. The time of the last successful fetch is kept in `kv`."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def replace_all(self, names: Mapping[str, str], fetched_at: datetime) -> None:
        """Swap in a fresh copy of the database, all or nothing."""
        stamp = to_db_time(fetched_at)
        async with self._db.transaction() as conn:
            await conn.execute("DELETE FROM item_names")
            await conn.executemany(
                "INSERT INTO item_names (unique_name, name) VALUES (?, ?)", list(names.items())
            )
            await conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?)"
                " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (ITEM_NAMES_FETCHED_AT_KEY, stamp),
            )

    async def all(self) -> dict[str, str]:
        async with self._db.conn.execute("SELECT unique_name, name FROM item_names") as cur:
            rows = await cur.fetchall()
        return {row["unique_name"]: row["name"] for row in rows}

    async def fetched_at(self) -> datetime | None:
        """When the cached names were fetched; None if they never were."""
        async with self._db.conn.execute(
            "SELECT value FROM kv WHERE key = ?", (ITEM_NAMES_FETCHED_AT_KEY,)
        ) as cur:
            row = await cur.fetchone()
        return from_db_time(row["value"]) if row else None


VOICE_HOLD_KEY = "voice_hold"
VoiceHoldReason = Literal["kicked", "moved"]


@dataclass(frozen=True)
class VoiceHold:
    """A moderator kicked Amir out of voice or dragged him elsewhere, so he stays out until an
    admin brings him back. When and how, nothing else: never who."""

    since: datetime
    reason: VoiceHoldReason


class VoiceHoldRepo:
    """The voice hold, kept in `kv` so it survives restarts: the time it was set (ISO-8601 UTC)
    and the reason, as JSON. It holds no member data."""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def get(self) -> VoiceHold | None:
        async with self._db.conn.execute(
            "SELECT value FROM kv WHERE key = ?", (VOICE_HOLD_KEY,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            return None
        data = json.loads(row["value"])
        return VoiceHold(cast(datetime, from_db_time(data["since"])), data["reason"])

    async def save(self, hold: VoiceHold) -> None:
        """Keep `hold`, replacing any earlier one."""
        value = json.dumps({"since": to_db_time(hold.since), "reason": hold.reason})
        async with self._db.transaction() as conn:
            await conn.execute(
                "INSERT INTO kv (key, value) VALUES (?, ?)"
                " ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (VOICE_HOLD_KEY, value),
            )

    async def clear(self) -> bool:
        """Lift the hold. True if there was one."""
        async with self._db.transaction() as conn:
            cur = await conn.execute("DELETE FROM kv WHERE key = ?", (VOICE_HOLD_KEY,))
            return cur.rowcount == 1
