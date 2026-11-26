"""Who is owed a greeting this stay: member ids and monotonic times only, in memory, always pruned.

A stay runs from joining voice to leaving it; leaving forgets the person entirely.
"""

import time
from collections.abc import Callable, Iterable

from amir.voice.rejoin import RejoinTracker


class StayTracker:
    def __init__(
        self,
        fresh_seconds: float,
        rejoin: RejoinTracker,
        *,
        greet_on_rejoin: bool,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._fresh = fresh_seconds
        self._rejoin = rejoin
        self._greet_on_rejoin = greet_on_rejoin
        self._clock = clock
        self._joined_at: dict[int, float] = {}
        self._met: set[int] = set()

    def joined_voice(self, member_id: int) -> None:
        self._joined_at[member_id] = self._clock()
        if not self._greet_on_rejoin and self._rejoin.is_rejoin(member_id):
            self._met.add(member_id)
        else:
            self._met.discard(member_id)

    def left_voice(self, member_id: int) -> None:
        self._joined_at.pop(member_id, None)
        self._met.discard(member_id)
        self._rejoin.record_leave(member_id)

    def owed(self, member_id: int) -> bool:
        return member_id in self._joined_at and member_id not in self._met

    def mark_met(self, member_ids: Iterable[int]) -> None:
        self._met.update(m for m in member_ids if m in self._joined_at)

    def fresh_since(self, member_id: int) -> float | None:
        joined = self._joined_at.get(member_id)
        if joined is None or self._clock() - joined > self._fresh:
            return None
        return joined

    def __len__(self) -> int:
        return len(self._joined_at)
