"""Short-lived rejoin memory: member ids and monotonic times only, in memory, always pruned."""

import time
from collections.abc import Callable


class RejoinTracker:
    def __init__(self, window_seconds: float, clock: Callable[[], float] = time.monotonic) -> None:
        self._window = window_seconds
        self._clock = clock
        self._left_at: dict[int, float] = {}

    def _prune(self) -> float:
        now = self._clock()
        self._left_at = {m: t for m, t in self._left_at.items() if now - t <= self._window}
        return now

    def record_leave(self, member_id: int) -> None:
        now = self._prune()
        self._left_at[member_id] = now

    def is_rejoin(self, member_id: int) -> bool:
        self._prune()
        return member_id in self._left_at

    def __len__(self) -> int:
        self._prune()
        return len(self._left_at)
