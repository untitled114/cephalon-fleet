import asyncio
import contextlib
import logging

import pytest

from amir.voice.queue import GreetingJob, GreetingQueue, OfferResult


class Clock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Player:
    """Fake play_one: records order, tracks concurrency, optionally fails on given ids."""

    def __init__(self, fail_on: frozenset[int] = frozenset()) -> None:
        self.played: list[int] = []
        self.active = 0
        self.max_active = 0
        self.fail_on = fail_on

    async def __call__(self, job: GreetingJob) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(0.01)
            if job.member_id in self.fail_on:
                raise RuntimeError("boom")
            self.played.append(job.member_id)
        finally:
            self.active -= 1


async def drain(queue: GreetingQueue, player: Player, *, run_for: float = 0.2) -> None:
    task = asyncio.create_task(queue.run(player))
    await asyncio.sleep(run_for)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_serial_fifo_no_overlap():
    clock = Clock()
    queue = GreetingQueue(capacity=5, expiry_seconds=30, clock=clock)
    for member in (1, 2, 3):
        assert queue.offer(GreetingJob(member, clock())) is OfferResult.ACCEPTED
    player = Player()
    await drain(queue, player)
    assert player.played == [1, 2, 3]
    assert player.max_active == 1


def test_capacity():
    clock = Clock()
    queue = GreetingQueue(capacity=5, expiry_seconds=30, clock=clock)
    results = [queue.offer(GreetingJob(m, clock())) for m in range(6)]
    assert results[:5] == [OfferResult.ACCEPTED] * 5
    assert results[5] is OfferResult.FULL
    assert len(queue) == 5


def test_full_is_logged_without_member_id(caplog):
    clock = Clock()
    queue = GreetingQueue(capacity=1, expiry_seconds=30, clock=clock)
    queue.offer(GreetingJob(111, 0.0))
    with caplog.at_level(logging.WARNING):
        assert queue.offer(GreetingJob(987654321, 0.0)) is OfferResult.FULL
    assert caplog.records
    assert all(r.levelno == logging.WARNING for r in caplog.records)
    assert "987654321" not in caplog.text and "111" not in caplog.text


async def test_duplicate_member_coalesced():
    clock = Clock()
    queue = GreetingQueue(capacity=5, expiry_seconds=30, clock=clock)
    assert queue.offer(GreetingJob(7, 0.0)) is OfferResult.ACCEPTED
    assert queue.offer(GreetingJob(8, 1.0)) is OfferResult.ACCEPTED
    assert queue.offer(GreetingJob(7, 2.0)) is OfferResult.DUPLICATE
    assert len(queue) == 2
    player = Player()
    await drain(queue, player)
    assert player.played == [7, 8]


async def test_duplicate_keeps_older_created_at():
    clock = Clock()
    queue = GreetingQueue(capacity=5, expiry_seconds=30, clock=clock)
    queue.offer(GreetingJob(7, 0.0))
    queue.offer(GreetingJob(7, 25.0))  # newer stamp must not replace the older one
    clock.now = 31.0
    player = Player()
    await drain(queue, player)
    assert player.played == []


async def test_expired_job_skipped(caplog):
    clock = Clock()
    queue = GreetingQueue(capacity=5, expiry_seconds=30, clock=clock)
    queue.offer(GreetingJob(424242, 0.0))
    clock.now = 31.0
    player = Player()
    with caplog.at_level(logging.INFO):
        await drain(queue, player)
    assert player.played == []
    assert len(queue) == 0
    assert "424242" not in caplog.text
    assert "expired" in caplog.text


async def test_job_at_exact_expiry_still_plays():
    clock = Clock()
    queue = GreetingQueue(capacity=5, expiry_seconds=30, clock=clock)
    queue.offer(GreetingJob(1, 0.0))
    clock.now = 30.0
    player = Player()
    await drain(queue, player)
    assert player.played == [1]


async def test_play_failure_does_not_stop_queue(caplog):
    clock = Clock()
    queue = GreetingQueue(capacity=5, expiry_seconds=30, clock=clock)
    queue.offer(GreetingJob(1, 0.0))
    queue.offer(GreetingJob(2, 0.0))
    player = Player(fail_on=frozenset({1}))
    with caplog.at_level(logging.ERROR):
        await drain(queue, player)
    assert player.played == [2]
    assert "RuntimeError" in caplog.text


async def test_member_can_requeue_while_playing():
    clock = Clock()
    queue = GreetingQueue(capacity=5, expiry_seconds=30, clock=clock)
    queue.offer(GreetingJob(5, 0.0))
    started = asyncio.Event()
    release = asyncio.Event()
    played: list[int] = []

    async def slow(job: GreetingJob) -> None:
        started.set()
        await release.wait()
        played.append(job.member_id)

    task = asyncio.create_task(queue.run(slow))
    await started.wait()
    assert queue.offer(GreetingJob(5, 0.0)) is OfferResult.ACCEPTED
    assert len(queue) == 1
    release.set()
    await asyncio.sleep(0.02)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    assert played == [5, 5]


async def test_cancel_propagates():
    queue = GreetingQueue(capacity=5, expiry_seconds=30, clock=Clock())
    task = asyncio.create_task(queue.run(Player()))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_clock_is_the_queues_own():
    clock = Clock(42.0)
    queue = GreetingQueue(capacity=1, expiry_seconds=30, clock=clock)

    assert queue.clock is clock
    assert queue.clock() == 42.0
