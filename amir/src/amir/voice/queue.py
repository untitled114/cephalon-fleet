"""Bounded, serial greeting queue: one clip at a time, never stale, never unbounded."""

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class GreetingJob:
    member_id: int
    created_at: float


class OfferResult(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    FULL = "full"


class GreetingQueue:
    def __init__(
        self,
        *,
        capacity: int,
        expiry_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._queue: asyncio.Queue[GreetingJob] = asyncio.Queue(maxsize=capacity)
        self._queued: set[int] = set()
        self._expiry = expiry_seconds
        self._clock = clock

    def __len__(self) -> int:
        return self._queue.qsize()

    @property
    def clock(self) -> Callable[[], float]:
        """The clock that judges expiry: a job's created_at must come from it."""
        return self._clock

    def offer(self, job: GreetingJob) -> OfferResult:
        if job.member_id in self._queued:
            return OfferResult.DUPLICATE
        if self._queue.full():
            log.warning("greeting dropped: queue full (queue length %d)", len(self))
            return OfferResult.FULL
        self._queue.put_nowait(job)
        self._queued.add(job.member_id)
        return OfferResult.ACCEPTED

    async def run(self, play_one: Callable[[GreetingJob], Awaitable[None]]) -> None:
        while True:
            job = await self._queue.get()
            # Cleared on take, so a member who rejoins mid-greeting can queue again.
            self._queued.discard(job.member_id)
            if self._clock() - job.created_at > self._expiry:
                log.info("greeting expired and skipped (queue length %d)", len(self))
                continue
            try:
                await play_one(job)
            except Exception as exc:
                log.error("greeting playback failed: %s", type(exc).__name__)
