"""Outbox worker: delivers queued posts through the ChannelWriter, at most once per dedup key.

Per row: claim it ('sending', its own short write), await the writer with no transaction
open, then record the outcome (its own short write). A row is 'sent' only after the writer
confirms it. A crash during the Discord call leaves the row 'sending'; `reconcile_inflight`
settles it at startup by looking for the message in the channel.

While publishing is off (the writer is a disabled gate) the row is settled as 'previewed'
in one write *before* the gate records the preview. 'previewed' is terminal, so a crash
there loses at most one preview row, and turning publishing on never replays anything.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from amir.clock import Clock
from amir.publishing.cards import Card
from amir.publishing.gate import ChannelWriter, SendResult
from amir.storage.repos import OutboxRepo, OutboxRow

log = logging.getLogger(__name__)

_ERROR_TEXT_LIMIT = 500


class OutboxWorker:
    def __init__(
        self,
        repo: OutboxRepo,
        writer: ChannelWriter,
        clock: Clock,
        *,
        max_attempts: int = 8,
        base_delay: float = 30,
        max_delay: float = 1800,
    ) -> None:
        self._repo = repo
        self._writer = writer
        self._clock = clock
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._max_delay = max_delay

    async def reconcile_inflight(self) -> None:
        """Settle rows a crash left 'sending': 'sent' if the message is found, else pending."""
        for row in await self._repo.inflight():
            found, reason = await self._look_up(row)
            if found is not None:
                await self._repo.mark_sent(row.id, found)
                log.info(
                    "outbox row %s (%s, %s): found as message %s after an interrupted send",
                    row.id,
                    row.kind,
                    row.dedup_key,
                    found,
                )
                continue
            await self._repo.mark_retry(row.id, reason, self._clock.now())
            log.info("outbox row %s (%s, %s): requeued", row.id, row.kind, row.dedup_key)

    async def _look_up(self, row: OutboxRow) -> tuple[int | None, str]:
        """(message id or None, why it will be requeued if None)."""
        try:
            identities = {Card.from_dict(c).identity() for c in row.payload.get("cards") or []}
            if not identities:
                return None, "interrupted while sending; a text-only post cannot be looked up"
            found = await self._writer.find_recent(row.channel, identities)
        except Exception as exc:
            log.warning(
                "outbox row %s (%s, %s): lookup after an interrupted send failed: %s",
                row.id,
                row.kind,
                row.dedup_key,
                type(exc).__name__,
            )
            return None, f"interrupted while sending; lookup failed: {_describe(exc)}"
        return found, "interrupted while sending; not found in the channel"

    async def drain_once(self) -> int:
        """Attempt every due row once. Returns how many rows were attempted."""
        previewing = getattr(self._writer, "enabled", True) is False
        claim = self._repo.claim_previewed if previewing else self._repo.mark_sending
        attempted = 0
        for row in await self._repo.due(self._clock.now()):
            if not await claim(row.id):
                continue  # no longer pending
            attempted += 1
            if previewing:
                await self._record_preview(row)
            else:
                await self._attempt(row, attempts=row.attempts + 1)
        return attempted

    async def run(self, interval: float = 5.0) -> None:
        """Drain every `interval` seconds. Database errors propagate to the task supervisor."""
        while True:
            await self.drain_once()
            await asyncio.sleep(interval)

    async def _send(self, row: OutboxRow) -> SendResult:
        payload = row.payload
        return await self._writer.send(
            row.channel,
            cards=[Card.from_dict(card) for card in payload.get("cards") or []],
            text=payload.get("text"),
            role_ping_id=payload.get("role_ping_id"),
        )

    async def _record_preview(self, row: OutboxRow) -> None:
        """The row is already 'previewed'; a failure here only loses the preview row."""
        try:
            await self._send(row)
        except Exception as exc:
            log.warning(
                "outbox row %s (%s, %s): preview not recorded: %s",
                row.id,
                row.kind,
                row.dedup_key,
                type(exc).__name__,
            )
            return
        log.debug("outbox row %s (%s, %s): previewed", row.id, row.kind, row.dedup_key)

    async def _attempt(self, row: OutboxRow, *, attempts: int) -> None:
        try:
            result = await self._send(row)
        except Exception as exc:  # any failure, PublishError or not, must not strand the row
            await self._retry_or_fail(row, attempts, exc)
            return
        if result.status == "previewed":
            await self._repo.mark_previewed(row.id)
            log.debug("outbox row %s (%s, %s): previewed", row.id, row.kind, row.dedup_key)
        else:
            await self._repo.mark_sent(row.id, result.message_id)
            log.info(
                "outbox row %s (%s, %s): sent as message %s",
                row.id,
                row.kind,
                row.dedup_key,
                result.message_id,
            )

    async def _retry_or_fail(self, row: OutboxRow, attempts: int, exc: Exception) -> None:
        error = _describe(exc)
        if attempts >= self._max_attempts:
            await self._repo.mark_failed(row.id, error)
            log.error(
                "outbox row %s (%s, %s) failed after %d attempts: %s",
                row.id,
                row.kind,
                row.dedup_key,
                attempts,
                type(exc).__name__,
            )
            return
        delay = min(self._base_delay * 2 ** (attempts - 1), self._max_delay)
        await self._repo.mark_retry(row.id, error, self._clock.now() + timedelta(seconds=delay))
        log.warning(
            "outbox row %s (%s, %s) attempt %d failed: %s; retrying in %ss",
            row.id,
            row.kind,
            row.dedup_key,
            attempts,
            type(exc).__name__,
            delay,
        )


def _describe(exc: BaseException) -> str:
    """Error text for the row's last_error column (kept in SQLite, never logged)."""
    return f"{type(exc).__name__}: {exc}"[:_ERROR_TEXT_LIMIT]
