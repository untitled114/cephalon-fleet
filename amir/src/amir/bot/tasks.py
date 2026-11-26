"""Supervised background tasks: one per name, restarted with backoff when they crash.

A task that returns normally is finished (e.g. the news pipeline with no source enabled) and is
not restarted; a routine one-shot (e.g. reading the audit log on each voice join) says so at
DEBUG only. A task that raises is logged with its traceback and restarted after 1, 2, 4 ...
seconds, capped at 60. A run that lasted longer than the delay now due resets it to 1 s, so a
task that crashes rarely is not held back by old crashes. Cancellation is never a crash.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

log = logging.getLogger(__name__)

FIRST_BACKOFF_SECONDS = 1
MAX_BACKOFF_SECONDS = 60

TaskFactory = Callable[[], Awaitable[None]]


class TaskSupervisor:
    def __init__(
        self,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sleep = sleep
        self._clock = clock
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._closed = False

    def start(self, name: str, factory: TaskFactory, *, routine: bool = False) -> bool:
        """Run `factory()` under supervision. False if `name` is already running, or after
        cancel_all (shutdown). A `routine` task logs its normal finish at DEBUG, not INFO."""
        if self._closed:
            log.debug("task %s not started: shutting down", name)
            return False
        current = self._tasks.get(name)
        if current is not None and not current.done():
            return False
        self._tasks[name] = asyncio.create_task(self._supervise(name, factory, routine), name=name)
        return True

    def running(self) -> list[str]:
        """Names of the supervised tasks still running (a crashed one waiting to restart
        counts), sorted."""
        return sorted(name for name, task in self._tasks.items() if not task.done())

    async def cancel_all(self) -> None:
        """Cancel every task and wait until each has finished; nothing starts afterwards."""
        self._closed = True
        tasks = list(self._tasks.values())
        self._tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _supervise(self, name: str, factory: TaskFactory, routine: bool) -> None:
        delay = FIRST_BACKOFF_SECONDS
        while True:
            started = self._clock()
            try:
                await factory()
            except Exception:  # CancelledError is not an Exception: it ends the task
                if self._clock() - started > delay:
                    delay = FIRST_BACKOFF_SECONDS  # it ran a good while: a fresh failure
                log.exception("task %s crashed; restarting in %ss", name, delay)
                await self._sleep(delay)
                delay = min(delay * 2, MAX_BACKOFF_SECONDS)
                continue
            log.log(logging.DEBUG if routine else logging.INFO, "task %s finished", name)
            return
