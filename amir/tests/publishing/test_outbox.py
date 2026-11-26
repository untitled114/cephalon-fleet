import asyncio
import logging
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from amir.publishing.cards import Card, CardField
from amir.publishing.gate import (
    ChannelKey,
    GatedWriter,
    PermissionDenied,
    SendResult,
    TransientPublishError,
)
from amir.publishing.outbox import OutboxWorker
from amir.storage.db import Database
from amir.storage.repos import OutboxRepo, PreviewRepo

# Titles, links and dates of the two newest items in the captured forum feed.
CARD = Card(
    title="Iceblade of Narin: The Icebind: Hotfix 44.1.3",
    url="https://forums.warframe.com/topic/1529796-iceblade-of-narin-the-icebind-hotfix-4413/",
    timestamp=datetime(2026, 10, 22, 19, 4, 17, tzinfo=UTC),
    fields=(CardField("Category", "Hotfix"),),
)
CARD2 = Card(
    title="Iceblade of Narin: The Icebind: Hotfix 44.1.2 + 2.1",
    url="https://forums.warframe.com/topic/1528620-iceblade-of-narin-the-icebind-hotfix-4412-21/",
    timestamp=datetime(2026, 10, 15, 14, 55, 4, tzinfo=UTC),
    fields=(CardField("Category", "Hotfix"),),
)
KEY = "news:forum-pc-updates:1529796"
KEY2 = "news:forum-pc-updates:1528620"
PAYLOAD: dict[str, Any] = {"cards": [CARD.to_dict()], "text": None, "role_ping_id": 7}
PAYLOAD2: dict[str, Any] = {"cards": [CARD2.to_dict()], "text": None, "role_ping_id": None}


class Hang:
    """Script step: block forever, as a Discord call would while the process is stopped."""


class ScriptedWriter:
    """A ChannelWriter whose send() plays back a script of results, exceptions and hangs.

    Each time it is reached it also records whether a DB transaction was open and which
    outbox rows were in flight, so tests can check what the worker did *before* the call.
    """

    def __init__(
        self,
        db: Database,
        repo: OutboxRepo,
        script: Sequence[SendResult | BaseException | Hang] = (),
        *,
        found: int | BaseException | None = None,
    ) -> None:
        self._db = db
        self._repo = repo
        self.script = list(script)
        self.found = found
        self.calls: list[tuple[Any, ...]] = []
        self.observed: list[tuple[bool, list[str]]] = []
        self.hanging = asyncio.Event()

    async def _observe(self) -> None:
        inflight = [row.dedup_key for row in await self._repo.inflight()]
        self.observed.append((self._db.conn.in_transaction, inflight))

    async def send(
        self,
        channel: ChannelKey,
        *,
        cards: Sequence[Card] = (),
        text: str | None = None,
        role_ping_id: int | None = None,
    ) -> SendResult:
        self.calls.append(("send", channel, list(cards), text, role_ping_id))
        await self._observe()
        step = self.script.pop(0)
        if isinstance(step, Hang):
            self.hanging.set()
            await asyncio.Event().wait()
        if isinstance(step, BaseException):
            raise step
        assert isinstance(step, SendResult)
        return step

    async def edit(
        self, channel: ChannelKey, message_id: int, *, cards: Sequence[Card]
    ) -> SendResult:
        raise AssertionError("the outbox never edits")

    async def find_recent(
        self, channel: ChannelKey, identities: set[tuple[str, str, str]], limit: int = 50
    ) -> int | None:
        self.calls.append(("find_recent", channel, identities))
        await self._observe()
        if isinstance(self.found, BaseException):
            raise self.found
        return self.found


class PreviewThenStop(PreviewRepo):
    """Commits the preview row, then blocks: the process stops before the worker resumes."""

    def __init__(self, db: Database, clock) -> None:
        super().__init__(db, clock)
        self.committed = asyncio.Event()

    async def add(self, channel: str, action: Any, payload: dict[str, Any]) -> None:
        await super().add(channel, action, payload)
        self.committed.set()
        await asyncio.Event().wait()


class BrokenPreviews(PreviewRepo):
    async def add(self, channel: str, action: Any, payload: dict[str, Any]) -> None:
        raise sqlite3.OperationalError("disk I/O error")


@pytest.fixture
def repo(db: Database, clock) -> OutboxRepo:
    return OutboxRepo(db, clock)


async def row_count(db: Database) -> int:
    async with db.conn.execute("SELECT COUNT(*) FROM outbox") as cur:
        row = await cur.fetchone()
    return int(row[0])


async def crash_mid_send(repo: OutboxRepo, key: str) -> None:
    """Leave a row as a crash during the Discord call would: claimed, never resolved."""
    row = await repo.get_by_key(key)
    assert row is not None
    assert await repo.mark_sending(row.id) is True


# --- Worker behaviour ------------------------------------------------------------------


async def test_marked_sent_only_after_confirmation(db, clock, repo) -> None:
    assert await repo.enqueue("news", KEY, ChannelKey.NEWS, PAYLOAD) is True
    writer = ScriptedWriter(
        db, repo, [TransientPublishError("503 Service Unavailable"), SendResult("sent", 55)]
    )
    worker = OutboxWorker(repo, writer, clock)
    first_try = clock.now()

    assert await worker.drain_once() == 1
    row = await repo.get_by_key(KEY)
    assert row is not None
    assert row.status == "pending"
    assert row.attempts == 1
    assert row.next_attempt_at == first_try + timedelta(seconds=30)
    assert row.message_id is None
    assert "TransientPublishError" in (row.last_error or "")

    clock.advance(29)
    assert await worker.drain_once() == 0  # still backing off
    clock.advance(1)
    assert await worker.drain_once() == 1
    row = await repo.get_by_key(KEY)
    assert row is not None
    assert row.status == "sent"
    assert row.message_id == 55
    assert row.attempts == 2

    # The writer was reached only with the row already 'sending' and no transaction open.
    assert writer.observed == [(False, [KEY]), (False, [KEY])]
    assert writer.calls == [("send", ChannelKey.NEWS, [CARD], None, 7)] * 2
    clock.advance(86_400)
    assert await worker.drain_once() == 0
    assert len(writer.calls) == 2


async def test_duplicate_dedup_key_ignored(db, repo) -> None:
    assert await repo.enqueue("news", KEY, ChannelKey.NEWS, PAYLOAD) is True
    assert await repo.enqueue("news", KEY, ChannelKey.NEWS, PAYLOAD2) is False

    assert await row_count(db) == 1
    row = await repo.get_by_key(KEY)
    assert row is not None
    assert row.payload == PAYLOAD  # the first enqueue wins


async def test_gated_rows_never_replayed_after_enable(db, clock, repo) -> None:
    sender = ScriptedWriter(db, repo, [SendResult("sent", 77)])
    previews = PreviewRepo(db, clock)
    await repo.enqueue("news", KEY, ChannelKey.NEWS, PAYLOAD)
    gated = OutboxWorker(repo, GatedWriter(sender, previews, enabled=False), clock)

    assert await gated.drain_once() == 1
    row = await repo.get_by_key(KEY)
    assert row is not None
    assert row.status == "previewed"
    assert row.message_id is None
    assert await previews.count() == 1

    # The owner turns publishing on and restarts: new gate, new worker, same database.
    clock.advance(3600)
    enabled = OutboxWorker(repo, GatedWriter(sender, previews, enabled=True), clock)
    await enabled.reconcile_inflight()
    assert await enabled.drain_once() == 0
    assert sender.calls == []
    row = await repo.get_by_key(KEY)
    assert row is not None
    assert row.status == "previewed"

    # Only news that arrives after the switch is posted.
    await repo.enqueue("news", KEY2, ChannelKey.NEWS, PAYLOAD2)
    assert await enabled.drain_once() == 1
    assert sender.calls == [("send", ChannelKey.NEWS, [CARD2], None, None)]
    row2 = await repo.get_by_key(KEY2)
    assert row2 is not None
    assert (row2.status, row2.message_id) == ("sent", 77)


async def test_permission_denied_backs_off_then_fails(db, clock, repo, caplog) -> None:
    caplog.set_level(logging.INFO)
    await repo.enqueue("news", KEY, ChannelKey.NEWS, PAYLOAD)
    writer = ScriptedWriter(
        db, repo, [PermissionDenied("403 Forbidden (error code: 50013)") for _ in range(8)]
    )
    worker = OutboxWorker(repo, writer, clock)

    delays: list[float] = []
    for _ in range(7):
        assert await worker.drain_once() == 1
        row = await repo.get_by_key(KEY)
        assert row is not None
        assert row.status == "pending"
        delay = (row.next_attempt_at - clock.now()).total_seconds()
        delays.append(delay)
        clock.advance(delay - 1)
        assert await worker.drain_once() == 0  # not due until the delay has passed
        clock.advance(1)
    assert delays == [30, 60, 120, 240, 480, 960, 1800]
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]

    assert await worker.drain_once() == 1
    row = await repo.get_by_key(KEY)
    assert row is not None
    assert row.status == "failed"
    assert row.attempts == 8
    assert "PermissionDenied" in (row.last_error or "")

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert KEY in errors[0].getMessage()
    assert "PermissionDenied" in errors[0].getMessage()
    assert CARD.title not in caplog.text  # no payload content in logs

    clock.advance(86_400)
    assert await worker.drain_once() == 0
    assert len(writer.calls) == 8
    assert await repo.inflight() == []


@pytest.mark.parametrize(
    "error",
    [
        ConnectionResetError("Connection reset by peer"),
        AttributeError("'NoneType' object has no attribute 'send'"),
        PermissionDenied("403 Forbidden"),
    ],
    ids=["network", "programming", "non-transient-publish-error"],
)
async def test_any_writer_exception_is_retried_never_stuck(
    db, clock, repo, caplog, error: Exception
) -> None:
    caplog.set_level(logging.INFO)
    await repo.enqueue("news", KEY, ChannelKey.NEWS, PAYLOAD)
    worker = OutboxWorker(repo, ScriptedWriter(db, repo, [error]), clock)

    assert await worker.drain_once() == 1

    assert await repo.inflight() == []
    row = await repo.get_by_key(KEY)
    assert row is not None
    assert row.status == "pending"
    assert row.attempts == 1
    assert row.next_attempt_at == clock.now() + timedelta(seconds=30)
    assert type(error).__name__ in (row.last_error or "")
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert type(error).__name__ in warnings[0].getMessage()
    assert KEY in warnings[0].getMessage()
    assert CARD.title not in caplog.text


async def test_previewed_result_from_any_writer_is_terminal(db, clock, repo) -> None:
    await repo.enqueue("news", KEY, ChannelKey.NEWS, PAYLOAD)
    writer = ScriptedWriter(db, repo, [SendResult("previewed", None)])
    worker = OutboxWorker(repo, writer, clock)

    assert await worker.drain_once() == 1
    row = await repo.get_by_key(KEY)
    assert row is not None
    assert (row.status, row.message_id) == ("previewed", None)
    clock.advance(86_400)
    assert await worker.drain_once() == 0
    assert len(writer.calls) == 1


async def test_retry_delay_capped_at_max_delay(db, clock, repo) -> None:
    await repo.enqueue("news", KEY, ChannelKey.NEWS, PAYLOAD)
    script = [TransientPublishError("429") for _ in range(4)]
    worker = OutboxWorker(
        repo, ScriptedWriter(db, repo, script), clock, max_attempts=5, base_delay=10, max_delay=50
    )

    delays: list[float] = []
    for _ in range(4):
        assert await worker.drain_once() == 1
        row = await repo.get_by_key(KEY)
        assert row is not None
        delays.append((row.next_attempt_at - clock.now()).total_seconds())
        clock.advance(delays[-1])
    assert delays == [10, 20, 40, 50]


async def test_reconcile_finds_existing_message(db, clock, repo) -> None:
    await repo.enqueue("news", KEY, ChannelKey.NEWS, PAYLOAD)
    await crash_mid_send(repo, KEY)
    writer = ScriptedWriter(db, repo, found=999)
    worker = OutboxWorker(repo, writer, clock)

    await worker.reconcile_inflight()

    row = await repo.get_by_key(KEY)
    assert row is not None
    assert row.status == "sent"
    assert row.message_id == 999
    assert await repo.inflight() == []
    assert await worker.drain_once() == 0
    assert writer.calls == [("find_recent", ChannelKey.NEWS, {CARD.identity()})]


async def test_reconcile_requeues_when_absent(db, clock, repo) -> None:
    await repo.enqueue("news", KEY, ChannelKey.NEWS, PAYLOAD)
    await crash_mid_send(repo, KEY)
    writer = ScriptedWriter(db, repo, [SendResult("sent", 56)], found=None)
    worker = OutboxWorker(repo, writer, clock)

    await worker.reconcile_inflight()

    row = await repo.get_by_key(KEY)
    assert row is not None
    assert row.status == "pending"
    assert row.attempts == 1  # the interrupted attempt still counts
    assert row.message_id is None
    assert await worker.drain_once() == 1
    row = await repo.get_by_key(KEY)
    assert row is not None
    assert (row.status, row.message_id) == ("sent", 56)
    assert writer.calls == [
        ("find_recent", ChannelKey.NEWS, {CARD.identity()}),
        ("send", ChannelKey.NEWS, [CARD], None, 7),
    ]


async def test_reconcile_uses_identities_of_every_card(db, clock, repo) -> None:
    long_title = Card(title=CARD.title * 6, url=CARD.url, timestamp=CARD.timestamp)
    payload = {"cards": [long_title.to_dict(), CARD2.to_dict()], "text": None, "role_ping_id": None}
    await repo.enqueue("news", KEY, ChannelKey.NEWS, payload)
    await crash_mid_send(repo, KEY)
    writer = ScriptedWriter(db, repo, found=1000)

    await OutboxWorker(repo, writer, clock).reconcile_inflight()

    assert writer.calls == [
        ("find_recent", ChannelKey.NEWS, {long_title.identity(), CARD2.identity()})
    ]
    _, _, identities = writer.calls[0]
    assert all(len(title) <= 256 for title, _, _ in identities)


async def test_reconcile_text_only_row_requeued_without_lookup(db, clock, repo) -> None:
    payload = {"cards": [], "text": "Baro Ki'Teer has arrived.", "role_ping_id": None}
    await repo.enqueue("baro", "baro:1792760400:arrival", ChannelKey.NEWS, payload)
    await crash_mid_send(repo, "baro:1792760400:arrival")
    writer = ScriptedWriter(db, repo, found=1234)

    await OutboxWorker(repo, writer, clock).reconcile_inflight()

    assert writer.calls == []
    row = await repo.get_by_key("baro:1792760400:arrival")
    assert row is not None
    assert row.status == "pending"


async def test_reconcile_lookup_failure_requeues(db, clock, repo, caplog) -> None:
    caplog.set_level(logging.INFO)
    await repo.enqueue("news", KEY, ChannelKey.NEWS, PAYLOAD)
    await crash_mid_send(repo, KEY)
    writer = ScriptedWriter(db, repo, found=TransientPublishError("502 Bad Gateway"))

    await OutboxWorker(repo, writer, clock).reconcile_inflight()

    row = await repo.get_by_key(KEY)
    assert row is not None
    assert row.status == "pending"
    assert await repo.inflight() == []
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("TransientPublishError" in r.getMessage() for r in warnings)


async def test_concurrent_drains_send_each_row_once(db, clock, repo) -> None:
    await repo.enqueue("news", KEY, ChannelKey.NEWS, PAYLOAD)
    writer = ScriptedWriter(db, repo, [SendResult("sent", 55)])
    first = OutboxWorker(repo, writer, clock)
    second = OutboxWorker(repo, writer, clock)

    attempted = await asyncio.gather(first.drain_once(), second.drain_once())

    assert sorted(attempted) == [0, 1]
    assert len(writer.calls) == 1
    row = await repo.get_by_key(KEY)
    assert row is not None
    assert (row.status, row.message_id, row.attempts) == ("sent", 55, 1)


async def test_stop_after_preview_commit_never_replays_after_enable(db, clock, repo) -> None:
    sender = ScriptedWriter(db, repo, [SendResult("sent", 77)], found=None)
    previews = PreviewThenStop(db, clock)
    await repo.enqueue("news", KEY, ChannelKey.NEWS, PAYLOAD)
    gated = OutboxWorker(repo, GatedWriter(sender, previews, enabled=False), clock)

    task = asyncio.create_task(gated.drain_once())
    await previews.committed.wait()
    task.cancel()  # the process stops between the preview commit and anything after it
    with pytest.raises(asyncio.CancelledError):
        await task

    assert await previews.count() == 1
    row = await repo.get_by_key(KEY)
    assert row is not None
    assert (row.status, row.attempts) == ("previewed", 1)
    assert await repo.inflight() == []

    # The owner enables publishing and restarts.
    enabled = OutboxWorker(repo, GatedWriter(sender, PreviewRepo(db, clock), enabled=True), clock)
    await enabled.reconcile_inflight()
    assert await enabled.drain_once() == 0
    assert sender.calls == []


async def test_failed_preview_write_loses_only_the_preview(db, clock, repo, caplog) -> None:
    caplog.set_level(logging.INFO)
    sender = ScriptedWriter(db, repo)
    await repo.enqueue("news", KEY, ChannelKey.NEWS, PAYLOAD)
    gated = OutboxWorker(repo, GatedWriter(sender, BrokenPreviews(db, clock), enabled=False), clock)

    assert await gated.drain_once() == 1

    row = await repo.get_by_key(KEY)
    assert row is not None
    assert row.status == "previewed"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "OperationalError" in warnings[0].getMessage()
    assert KEY in warnings[0].getMessage()
    clock.advance(86_400)
    assert await gated.drain_once() == 0
    assert sender.calls == []


async def test_cancelled_send_stays_sending_until_reconciled(db, clock, repo) -> None:
    await repo.enqueue("news", KEY, ChannelKey.NEWS, PAYLOAD)
    hung = ScriptedWriter(db, repo, [Hang()])
    task = asyncio.create_task(OutboxWorker(repo, hung, clock).drain_once())
    await hung.hanging.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    row = await repo.get_by_key(KEY)
    assert row is not None
    assert (row.status, row.attempts) == ("sending", 1)
    assert not db.conn.in_transaction

    # After the restart the message turns out to have been posted.
    restarted = ScriptedWriter(db, repo, found=4242)
    await OutboxWorker(repo, restarted, clock).reconcile_inflight()
    row = await repo.get_by_key(KEY)
    assert row is not None
    assert (row.status, row.message_id) == ("sent", 4242)
    assert restarted.calls == [("find_recent", ChannelKey.NEWS, {CARD.identity()})]


async def test_run_drains_until_cancelled(db, clock, repo) -> None:
    await repo.enqueue("news", KEY, ChannelKey.NEWS, PAYLOAD)
    writer = ScriptedWriter(db, repo, [SendResult("sent", 88)])
    task = asyncio.create_task(OutboxWorker(repo, writer, clock).run(interval=0))
    try:
        for _ in range(200):
            row = await repo.get_by_key(KEY)
            if row is not None and row.status == "sent":
                break
            await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    row = await repo.get_by_key(KEY)
    assert row is not None
    assert (row.status, row.message_id) == ("sent", 88)
    assert len(writer.calls) == 1


# --- Repository ------------------------------------------------------------------------


async def test_enqueued_row_shape(repo, clock) -> None:
    await repo.enqueue("news", KEY, ChannelKey.NEWS, PAYLOAD)

    row = await repo.get_by_key(KEY)
    assert row is not None
    assert row.id > 0
    assert row.kind == "news"
    assert row.dedup_key == KEY
    assert row.channel is ChannelKey.NEWS
    assert row.payload == PAYLOAD
    assert row.status == "pending"
    assert row.attempts == 0
    assert row.next_attempt_at is None
    assert row.message_id is None
    assert row.last_error is None
    assert row.created_at == row.updated_at == clock.now()
    assert await repo.get_by_key("news:forum-pc-updates:0") is None


@pytest.mark.parametrize("channel", [ChannelKey.VOICE_LOG, "voice_log"])
async def test_enqueue_rejects_voice_log(db, repo, channel) -> None:
    payload = {"cards": [], "text": "Ordis has arrived.", "role_ping_id": None}
    with pytest.raises(ValueError, match="voice"):
        await repo.enqueue("voice", "voice:1", channel, payload)
    assert await row_count(db) == 0


class SourceWritesFailed(Exception):
    pass


async def test_enqueue_joins_callers_transaction(db, repo) -> None:
    with pytest.raises(SourceWritesFailed):
        async with db.transaction():
            assert await repo.enqueue("news", KEY, ChannelKey.NEWS, PAYLOAD) is True
            raise SourceWritesFailed
    assert await row_count(db) == 0

    async with db.transaction():
        assert await repo.enqueue("news", KEY, ChannelKey.NEWS, PAYLOAD) is True
        assert await repo.enqueue("news", KEY, ChannelKey.NEWS, PAYLOAD) is False
    assert await row_count(db) == 1


async def test_enqueue_rejects_unknown_channel(db, repo) -> None:
    with pytest.raises(ValueError):
        await repo.enqueue("news", KEY, "general", PAYLOAD)  # type: ignore[arg-type]
    assert await row_count(db) == 0


async def test_repo_defaults_to_system_clock(db) -> None:
    repo = OutboxRepo(db)
    before = datetime.now(UTC)
    await repo.enqueue("news", KEY, ChannelKey.NEWS, PAYLOAD)
    row = await repo.get_by_key(KEY)
    assert row is not None
    assert before <= row.created_at <= datetime.now(UTC)


async def test_due_filters_orders_and_limits(repo, clock) -> None:
    now = clock.now()
    for n in range(4):
        await repo.enqueue("news", f"news:forum-pc-updates:{n}", ChannelKey.NEWS, PAYLOAD)
    rows = {row.dedup_key: row for row in await repo.due(now)}
    first, second, third, fourth = (rows[f"news:forum-pc-updates:{n}"] for n in range(4))

    # Sub-second backoff must compare correctly against whole-second clock readings.
    await repo.mark_sending(second.id)
    await repo.mark_retry(second.id, "TransientPublishError: 429", now + timedelta(seconds=0.5))
    await repo.mark_sending(third.id)
    await repo.mark_sent(third.id, 3)
    await repo.mark_sending(fourth.id)  # in flight

    assert [row.dedup_key for row in await repo.due(now)] == [first.dedup_key]
    later = now + timedelta(seconds=1)
    assert [row.id for row in await repo.due(later)] == [first.id, second.id]
    assert [row.id for row in await repo.due(later, limit=1)] == [first.id]
    assert [row.id for row in await repo.inflight()] == [fourth.id]


async def test_claim_previewed_settles_a_pending_row_once(repo) -> None:
    await repo.enqueue("news", KEY, ChannelKey.NEWS, PAYLOAD)
    row = await repo.get_by_key(KEY)
    assert row is not None

    assert await repo.claim_previewed(row.id) is True
    assert await repo.claim_previewed(row.id) is False
    assert await repo.mark_sending(row.id) is False
    settled = await repo.get_by_key(KEY)
    assert settled is not None
    assert (settled.status, settled.attempts) == ("previewed", 1)


async def test_mark_sending_claims_a_pending_row_once(repo) -> None:
    await repo.enqueue("news", KEY, ChannelKey.NEWS, PAYLOAD)
    row = await repo.get_by_key(KEY)
    assert row is not None

    assert await repo.mark_sending(row.id) is True
    assert await repo.mark_sending(row.id) is False
    claimed = await repo.get_by_key(KEY)
    assert claimed is not None
    assert (claimed.status, claimed.attempts) == ("sending", 1)


async def test_terminal_marks(repo, clock) -> None:
    await repo.enqueue("news", KEY, ChannelKey.NEWS, PAYLOAD)
    await repo.enqueue("news", KEY2, ChannelKey.NEWS, PAYLOAD2)
    first = await repo.get_by_key(KEY)
    second = await repo.get_by_key(KEY2)
    assert first is not None and second is not None

    clock.advance(60)
    await repo.mark_sending(first.id)
    await repo.mark_previewed(first.id)
    await repo.mark_sending(second.id)
    await repo.mark_failed(second.id, "PermissionDenied: 403 Forbidden")

    previewed = await repo.get_by_key(KEY)
    failed = await repo.get_by_key(KEY2)
    assert previewed is not None and failed is not None
    assert (previewed.status, previewed.message_id) == ("previewed", None)
    assert previewed.updated_at == clock.now()
    assert previewed.created_at == clock.now() - timedelta(seconds=60)
    assert (failed.status, failed.last_error) == ("failed", "PermissionDenied: 403 Forbidden")


async def test_counts_by_status(repo) -> None:
    statuses = ("pending", "sending", "sent", "previewed", "failed")
    assert await repo.counts_by_status() == dict.fromkeys(statuses, 0)
    await repo.enqueue("news", KEY, ChannelKey.NEWS, PAYLOAD)
    await repo.enqueue("news", KEY2, ChannelKey.NEWS, PAYLOAD2)
    row = await repo.get_by_key(KEY)
    assert row is not None
    await repo.claim_previewed(row.id)

    counts = await repo.counts_by_status()

    assert list(counts) == list(statuses)  # every status, in lifecycle order
    assert counts == {**dict.fromkeys(statuses, 0), "pending": 1, "previewed": 1}
