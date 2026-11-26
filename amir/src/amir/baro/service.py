"""Baro Ki'Teer's milestones queued for the news channel, plus the late-stock edit.

Each tick queues the milestone that is due (with catch-up, see `due_milestones`), at most once
per visit: a milestone counts as handled once its outbox row exists, whatever its status. An
arrival's follow-up messages are queued in the same transaction as the arrival itself.

If the stock wasn't listed when the arrival card was queued, a later tick that sees it edits
the arrival message in place (or records the edit as a preview while publishing is off), queues
any follow-up messages, and marks the visit so this happens once. No transaction spans the edit.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal

from amir.baro.cards import (
    arrival_stock,
    baro_arrival_messages,
    baro_departure_card,
    baro_warning_card,
    relay_name,
)
from amir.baro.names import ItemNameIndex
from amir.baro.schedule import Milestone, due_milestones
from amir.clock import Clock
from amir.config.settings import BaroSettings
from amir.publishing.cards import Card
from amir.publishing.gate import ChannelKey, ChannelWriter, PublishError
from amir.storage.repos import BaroVisitRepo, OutboxRepo, OutboxRow
from amir.worldstate.official import BaroVisit

log = logging.getLogger(__name__)

OUTBOX_KIND = "baro"
ROLE_PING_CATEGORY = "Baro Ki'Teer"

EditOutcome = Literal["edited", "retry", "failed"]


def _key(visit: BaroVisit, milestone: Milestone) -> str:
    return f"baro:{int(visit.activation.timestamp())}:{milestone}"


def _payload(cards: Sequence[Card], role_ping_id: int | None) -> dict[str, Any]:
    return {"cards": [card.to_dict() for card in cards], "text": None, "role_ping_id": role_ping_id}


class BaroService:
    def __init__(
        self,
        outbox: OutboxRepo,
        visits: BaroVisitRepo,
        writer: ChannelWriter,
        settings: BaroSettings,
        clock: Clock,
        *,
        notification_role_id: int = 0,
        role_ping_categories: Sequence[str] = (),
    ) -> None:
        self._outbox = outbox
        self._visits = visits
        self._writer = writer
        self._settings = settings
        self._clock = clock
        ping = notification_role_id != 0 and ROLE_PING_CATEGORY in role_ping_categories
        self._role_ping_id = notification_role_id if ping else None

    async def tick(
        self,
        visit: BaroVisit | None,
        wfcd_location: tuple[datetime, str] | None,
        wfcd_names: Mapping[str, str],
        index: ItemNameIndex,
    ) -> list[Milestone]:
        """Queue whatever milestone is due; returns the milestones queued by this tick."""
        if visit is None:
            return []
        location = relay_name(visit.node, wfcd_location, visit.activation)
        already = {
            m for m in Milestone if await self._outbox.get_by_key(_key(visit, m)) is not None
        }
        due = due_milestones(
            visit,
            self._clock.now(),
            already,
            warn_hours=self._settings.warn_before_arrival_hours,
            depart_hours=self._settings.warn_before_departure_hours,
        )
        queued = [m for m in due if await self._enqueue(m, visit, location, wfcd_names, index)]
        await self._late_stock(visit, location, wfcd_names, index)
        return queued

    async def _enqueue(
        self,
        milestone: Milestone,
        visit: BaroVisit,
        location: str,
        wfcd_names: Mapping[str, str],
        index: ItemNameIndex,
    ) -> bool:
        key = _key(visit, milestone)
        if milestone is Milestone.ARRIVAL:
            stock = arrival_stock(visit, index, wfcd_names) if visit.manifest else None
            first, *overflow = baro_arrival_messages(visit, location, stock)
            payload = {**_payload(first, self._role_ping_id), "stock_included": stock is not None}
            async with self._visits.db.transaction():
                queued = await self._outbox.enqueue(OUTBOX_KIND, key, ChannelKey.NEWS, payload)
                if queued:
                    await self._enqueue_overflow(key, overflow)
        else:
            build = baro_warning_card if milestone is Milestone.WARNING_72H else baro_departure_card
            payload = _payload([build(visit, location)], self._role_ping_id)
            queued = await self._outbox.enqueue(OUTBOX_KIND, key, ChannelKey.NEWS, payload)
        if queued:
            log.info("baro milestone queued key=%s", key)
        return queued

    async def _enqueue_overflow(self, arrival_key: str, messages: Sequence[list[Card]]) -> None:
        """Follow-up stock messages, keyed `{arrival_key}:{n}` for n >= 2; they never ping."""
        for n, cards in enumerate(messages, start=2):
            await self._outbox.enqueue(
                OUTBOX_KIND, f"{arrival_key}:{n}", ChannelKey.NEWS, _payload(cards, None)
            )

    async def _late_stock(
        self,
        visit: BaroVisit,
        location: str,
        wfcd_names: Mapping[str, str],
        index: ItemNameIndex,
    ) -> None:
        if not visit.manifest:
            return
        key = _key(visit, Milestone.ARRIVAL)
        row = await self._outbox.get_by_key(key)
        if row is None or row.payload.get("stock_included", True) is not False:
            return
        if await self._visits.stock_posted(visit.activation):
            return
        enabled = self._publishing_enabled()
        if enabled and row.status in ("pending", "sending"):
            return  # not delivered yet: edit it on a later tick, once it is sent
        first, *overflow = baro_arrival_messages(
            visit, location, arrival_stock(visit, index, wfcd_names)
        )
        sent = row.status == "sent"
        # A sent message is edited. Unsent with publishing off, the edit becomes a preview;
        # previewed or failed with publishing on, nothing exists to edit.
        outcome = "skipped"
        if sent or not enabled:
            outcome = await self._edit(row, first)
            if outcome == "retry":
                return
        async with self._visits.db.transaction():
            if sent and outcome == "edited":
                await self._enqueue_overflow(key, overflow)
            await self._visits.mark_stock_posted(visit.activation)
        log.info("baro late stock done key=%s row_status=%s edit=%s", key, row.status, outcome)

    async def _edit(self, row: OutboxRow, cards: list[Card]) -> EditOutcome:
        """Edit the arrival message in place; "retry" means try again next tick."""
        try:
            await self._writer.edit(ChannelKey.NEWS, row.message_id or 0, cards=cards)
        except PublishError as exc:
            if exc.transient:
                log.warning(
                    "baro late-stock edit failed key=%s error=%s; retrying next tick",
                    row.dedup_key,
                    type(exc).__name__,
                )
                return "retry"
            # Deleted message or missing permission: retrying every tick would not help.
            log.error(
                "baro late-stock edit failed key=%s error=%s; giving up on this visit's stock",
                row.dedup_key,
                type(exc).__name__,
            )
            return "failed"
        return "edited"

    def _publishing_enabled(self) -> bool:
        return getattr(self._writer, "enabled", True) is not False
