"""Batched voice log: join/leave lines held in memory and sent through the publishing gate.

Lines carry member mentions, so they are never logged and never stored. They reach Discord
only through GatedWriter and DiscordSender, which sends AllowedMentions.none().
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable

from amir.publishing.cards import CONTENT
from amir.publishing.gate import ChannelKey, ChannelWriter
from amir.publishing.sanitize import truncate

log = logging.getLogger(__name__)

# Lines waiting to be sent. Past this, the oldest go: after a long outage, a burst of
# minutes-old joins and leaves would mislead more than it informs.
MAX_PENDING = 50


class VoiceAnnouncer:
    def __init__(
        self,
        writer: ChannelWriter,
        *,
        min_interval: float = 2.0,
        max_lines: int = 10,
        max_attempts: int = 3,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        self._writer = writer
        self._min_interval = min_interval
        self._max_lines = max_lines
        self._max_attempts = max_attempts
        self._sleep = sleep
        self._pending: list[str] = []
        self._wakeup = asyncio.Event()

    def announce(self, line: str) -> None:
        """Queue one line for the voice log. Never blocks and never touches Discord."""
        self._pending.append(line)
        overflow = len(self._pending) - MAX_PENDING
        if overflow > 0:
            del self._pending[:overflow]
            _report_drop(overflow, f"over the {MAX_PENDING}-line backlog, oldest first")
        self._wakeup.set()

    async def run(self) -> None:
        while True:
            await self._wakeup.wait()
            batch = self._next_batch()
            if not self._pending:
                self._wakeup.clear()
            await self._deliver(batch)
            await self._sleep(self._min_interval)

    def _next_batch(self) -> list[str]:
        """Up to max_lines pending lines that fit one message; the rest wait their turn."""
        batch = [truncate(self._pending.pop(0), CONTENT)]
        size = len(batch[0])
        while self._pending and len(batch) < self._max_lines:
            size += 1 + len(self._pending[0])  # a newline, then the line
            if size > CONTENT:
                break
            batch.append(self._pending.pop(0))
        return batch

    async def _deliver(self, batch: list[str]) -> None:
        text = "\n".join(batch)
        attempt = 1
        while True:
            try:
                await self._writer.send(ChannelKey.VOICE_LOG, text=text)
                return
            except Exception as exc:  # CancelledError is not an Exception: it propagates
                # PublishError from the sender, or a network error it did not translate:
                # both get the same bounded retry, and a drop is always logged.
                if attempt >= self._max_attempts:
                    _report_drop(len(batch), f"after {attempt} attempt(s): {type(exc).__name__}")
                    return
            attempt += 1
            await self._sleep(self._min_interval)


def _report_drop(count: int, reason: str) -> None:
    # The lines carry member mentions, so only their number is logged.
    log.warning("dropped %d voice log line(s) %s", count, reason)
