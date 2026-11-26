import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

import pytest

from amir.baro.cards import arrival_stock
from amir.baro.names import ItemNameIndex
from amir.baro.schedule import Milestone, milestone_times
from amir.baro.service import BaroService
from amir.config.settings import BaroSettings
from amir.publishing.cards import MESSAGE_TOTAL, Card, message_chars
from amir.publishing.gate import (
    ChannelKey,
    GatedWriter,
    MessageMissing,
    SendResult,
    TransientPublishError,
)
from amir.storage.db import Database
from amir.storage.repos import BaroVisitRepo, OutboxRepo, PreviewRepo
from amir.worldstate.official import parse_void_trader
from amir.worldstate.wfcd import parse_baro_location, parse_inventory_names, parse_item_names

from .conftest import copies, stock_lines

ARRIVAL_KEY = "baro:1792760400:arrival"  # Unix time of the captured activation, 2026-10-23T13:00Z
DEPART_KEY = "baro:1792760400:depart10"
WARNING_KEY = "baro:1792760400:warn72"


class RecordingSender:
    """A ChannelWriter that records calls; `edit_error` makes edit() raise it."""

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.edit_error: Exception | None = None

    async def send(
        self,
        channel: ChannelKey,
        *,
        cards: Sequence[Card] = (),
        text: str | None = None,
        role_ping_id: int | None = None,
    ) -> SendResult:
        self.calls.append(("send", channel, list(cards)))
        return SendResult("sent", 42)

    async def edit(
        self, channel: ChannelKey, message_id: int, *, cards: Sequence[Card]
    ) -> SendResult:
        self.calls.append(("edit", channel, message_id, list(cards)))
        if self.edit_error is not None:
            raise self.edit_error
        return SendResult("sent", message_id)

    async def find_recent(
        self, channel: ChannelKey, identities: set[tuple[str, str, str]], limit: int = 50
    ) -> int | None:
        return None

    def edits(self) -> list[tuple[Any, ...]]:
        return [call for call in self.calls if call[0] == "edit"]


@dataclass
class Rig:
    service: BaroService
    outbox: OutboxRepo
    visits: BaroVisitRepo
    previews: PreviewRepo
    sender: RecordingSender


def make_rig(db: Database, clock: Any, *, enabled: bool = True, **kwargs: Any) -> Rig:
    sender = RecordingSender()
    previews = PreviewRepo(db, clock)
    outbox = OutboxRepo(db, clock)
    visits = BaroVisitRepo(db)
    writer = GatedWriter(sender, previews, enabled=enabled)
    service = BaroService(outbox, visits, writer, BaroSettings(), clock, **kwargs)
    return Rig(service, outbox, visits, previews, sender)


@pytest.fixture
def real(captured):
    visit = parse_void_trader(captured("official_worldstate_baro_present.json"))
    wfcd = captured("wfcd_pc_baro_present.json")
    index = ItemNameIndex(parse_item_names(captured("wfcd_items_names.json")))
    return visit, parse_baro_location(wfcd), parse_inventory_names(wfcd), index


async def _keys(db: Database) -> list[str]:
    async with db.conn.execute("SELECT dedup_key FROM outbox ORDER BY id") as cur:
        return [row[0] for row in await cur.fetchall()]


async def _cards(outbox: OutboxRepo, key: str) -> list[Card]:
    row = await outbox.get_by_key(key)
    assert row is not None
    return [Card.from_dict(card) for card in row.payload["cards"]]


def _stock_in(cards: Sequence[Card], lines: list[str]) -> list[str]:
    wanted = set(lines)
    return [line for card in cards for line in card.description.split("\n") if line in wanted]


def _big(visit, index, names):
    """The real visit with its manifest repeated until its mods need several messages."""
    mods, _ = arrival_stock(visit, index, names)
    return replace(visit, manifest=visit.manifest * copies(mods))


async def test_arrival_card_with_real_stock(db, clock, real):
    visit, location, names, index = real
    rig = make_rig(db, clock)
    assert clock.now() == datetime(2026, 10, 25, 1, 21, tzinfo=UTC)

    assert await rig.service.tick(visit, location, names, index) == [Milestone.ARRIVAL]

    assert await _keys(db) == [ARRIVAL_KEY]
    row = await rig.outbox.get_by_key(ARRIVAL_KEY)
    assert row.channel is ChannelKey.NEWS
    assert row.payload["role_ping_id"] is None
    assert row.payload["stock_included"] is True
    cards = await _cards(rig.outbox, ARRIVAL_KEY)
    text = "\n".join(card.title + "\n" + card.description for card in cards)
    assert "Strata Relay (Earth)" in text
    assert "Primed Fast Hands" in text
    assert "Eskhatos Ki'Teer Floof" not in text
    assert message_chars(cards) <= MESSAGE_TOTAL
    expected = stock_lines(visit, index, names)
    assert len(expected) == 12  # 11 mods and one summary line
    assert expected[-1].endswith("Glyphs 3 · Weapons 3")
    assert _stock_in(cards, expected) == expected
    assert rig.sender.calls == []


async def test_tick_is_idempotent(db, clock, real):
    visit, location, names, index = real
    rig = make_rig(db, clock)
    await rig.service.tick(visit, location, names, index)

    assert await rig.service.tick(visit, location, names, index) == []

    assert await _keys(db) == [ARRIVAL_KEY]
    assert rig.sender.calls == []


async def test_departure_warning_later(db, clock, real):
    visit, location, names, index = real
    rig = make_rig(db, clock)
    await rig.service.tick(visit, location, names, index)
    clock.advance((datetime(2026, 10, 25, 3, 30, tzinfo=UTC) - clock.now()).total_seconds())

    assert await rig.service.tick(visit, location, names, index) == [Milestone.DEPARTURE_10H]

    assert await _keys(db) == [ARRIVAL_KEY, DEPART_KEY]
    (card,) = await _cards(rig.outbox, DEPART_KEY)
    expiry = int(visit.expiry.timestamp())
    assert f"<t:{expiry}:F>" in card.description
    assert f"<t:{expiry}:R>" in card.description
    assert "Strata Relay (Earth)" in card.description


async def test_warning_72h_before_arrival(db, make_clock, real):
    visit, location, names, index = real
    clock = make_clock(milestone_times(visit)[Milestone.WARNING_72H])
    rig = make_rig(db, clock)

    assert await rig.service.tick(visit, location, names, index) == [Milestone.WARNING_72H]

    assert await _keys(db) == [WARNING_KEY]
    (card,) = await _cards(rig.outbox, WARNING_KEY)
    activation = int(visit.activation.timestamp())
    assert f"<t:{activation}:F>" in card.description
    assert f"<t:{activation}:R>" in card.description
    assert card.timestamp == visit.activation


async def test_large_stock_overflows_into_follow_up_messages(db, clock, real):
    visit, location, names, index = real
    big = _big(visit, index, names)
    rig = make_rig(db, clock, notification_role_id=4242, role_ping_categories=["Baro Ki'Teer"])

    assert await rig.service.tick(big, location, names, index) == [Milestone.ARRIVAL]

    keys = await _keys(db)
    assert len(keys) > 1
    assert keys == [ARRIVAL_KEY] + [f"{ARRIVAL_KEY}:{n}" for n in range(2, len(keys) + 1)]
    cards: list[Card] = []
    for key in keys:
        row = await rig.outbox.get_by_key(key)
        assert row.payload["role_ping_id"] == (4242 if key == ARRIVAL_KEY else None)
        message = await _cards(rig.outbox, key)
        assert message_chars(message) <= MESSAGE_TOTAL
        cards.extend(message)
    expected = stock_lines(big, index, names)
    assert _stock_in(cards, expected) == expected


class FailingOverflowOutbox(OutboxRepo):
    """An outbox whose first follow-up insert fails, as a full disk would."""

    async def enqueue(self, kind, dedup_key, channel, payload):  # type: ignore[override]
        if dedup_key.endswith(":2"):
            raise sqlite3.OperationalError("database or disk is full")
        return await super().enqueue(kind, dedup_key, channel, payload)


async def test_arrival_and_follow_ups_are_queued_atomically(db, clock, real):
    visit, location, names, index = real
    big = _big(visit, index, names)
    failing = BaroService(
        FailingOverflowOutbox(db, clock),
        BaroVisitRepo(db),
        GatedWriter(RecordingSender(), PreviewRepo(db, clock), enabled=True),
        BaroSettings(),
        clock,
    )

    with pytest.raises(sqlite3.OperationalError):
        await failing.tick(big, location, names, index)
    assert await _keys(db) == []

    rig = make_rig(db, clock)
    assert await rig.service.tick(big, location, names, index) == [Milestone.ARRIVAL]
    assert (await _keys(db))[:2] == [ARRIVAL_KEY, f"{ARRIVAL_KEY}:2"]


async def test_late_stock_edits_arrival(db, clock, real):
    visit, location, names, index = real
    rig = make_rig(db, clock)
    bare = replace(visit, manifest=())
    expected = stock_lines(visit, index, names)

    assert await rig.service.tick(bare, location, names, index) == [Milestone.ARRIVAL]
    row = await rig.outbox.get_by_key(ARRIVAL_KEY)
    assert row.payload["stock_included"] is False
    location_only = await _cards(rig.outbox, ARRIVAL_KEY)
    assert "Strata Relay (Earth)" in location_only[0].title
    assert _stock_in(location_only, expected) == []
    await rig.outbox.mark_sent(row.id, 77)

    assert await rig.service.tick(visit, location, names, index) == []

    edits = rig.sender.edits()
    assert len(edits) == 1
    _, channel, message_id, cards = edits[0]
    assert (channel, message_id) == (ChannelKey.NEWS, 77)
    assert _stock_in(cards, expected) == expected
    edited = "\n".join(card.description for card in cards)
    assert "Also aboard: Skins and armour 28" in edited
    assert "Eskhatos Pauldrons" not in edited
    assert await rig.visits.stock_posted(visit.activation) is True
    assert await _keys(db) == [ARRIVAL_KEY]

    await rig.service.tick(visit, location, names, index)
    assert len(rig.sender.edits()) == 1


async def test_late_stock_overflow_is_queued_after_the_edit(db, clock, real):
    visit, location, names, index = real
    big = _big(visit, index, names)
    rig = make_rig(db, clock)
    await rig.service.tick(replace(visit, manifest=()), location, names, index)
    await rig.outbox.mark_sent((await rig.outbox.get_by_key(ARRIVAL_KEY)).id, 77)

    await rig.service.tick(big, location, names, index)

    (edit,) = rig.sender.edits()
    keys = await _keys(db)
    assert keys[0] == ARRIVAL_KEY
    assert keys[1:] == [f"{ARRIVAL_KEY}:{n}" for n in range(2, len(keys) + 1)]
    assert len(keys) > 1
    cards = list(edit[3])
    for key in keys[1:]:
        cards.extend(await _cards(rig.outbox, key))
    expected = stock_lines(big, index, names)
    assert _stock_in(cards, expected) == expected


async def test_late_stock_with_gate_off_records_an_edit_preview(db, clock, real):
    visit, location, names, index = real
    rig = make_rig(db, clock, enabled=False)
    await rig.service.tick(replace(visit, manifest=()), location, names, index)
    row = await rig.outbox.get_by_key(ARRIVAL_KEY)
    assert await rig.outbox.claim_previewed(row.id)  # as the outbox worker does when gated

    await rig.service.tick(visit, location, names, index)

    edits = [p for p in await rig.previews.recent(10) if p["action"] == "edit"]
    assert len(edits) == 1
    assert edits[0]["channel"] == ChannelKey.NEWS
    assert edits[0]["payload"]["message_id"] == 0
    cards = [Card.from_dict(card) for card in edits[0]["payload"]["cards"]]
    expected = stock_lines(visit, index, names)
    assert _stock_in(cards, expected) == expected
    assert rig.sender.calls == []
    assert await rig.visits.stock_posted(visit.activation) is True


async def test_late_stock_waits_for_a_pending_arrival_to_be_sent(db, clock, real):
    visit, location, names, index = real
    rig = make_rig(db, clock)
    await rig.service.tick(replace(visit, manifest=()), location, names, index)
    row = await rig.outbox.get_by_key(ARRIVAL_KEY)
    assert row.status == "pending"

    await rig.service.tick(visit, location, names, index)

    assert rig.sender.calls == []
    assert await rig.visits.stock_posted(visit.activation) is False

    await rig.outbox.mark_sent(row.id, 77)
    await rig.service.tick(visit, location, names, index)

    (edit,) = rig.sender.edits()
    assert edit[1:3] == (ChannelKey.NEWS, 77)
    expected = stock_lines(visit, index, names)
    assert _stock_in(edit[3], expected) == expected
    assert await rig.visits.stock_posted(visit.activation) is True


async def test_late_stock_waits_while_the_arrival_is_sending(db, clock, real):
    visit, location, names, index = real
    rig = make_rig(db, clock)
    await rig.service.tick(replace(visit, manifest=()), location, names, index)
    assert await rig.outbox.mark_sending((await rig.outbox.get_by_key(ARRIVAL_KEY)).id)

    await rig.service.tick(visit, location, names, index)

    assert rig.sender.calls == []
    assert await rig.visits.stock_posted(visit.activation) is False


async def test_late_stock_with_gate_on_and_previewed_row_skips_the_edit(db, clock, real):
    visit, location, names, index = real
    rig = make_rig(db, clock)
    await rig.service.tick(replace(visit, manifest=()), location, names, index)
    # Previewed during an earlier gated run; publishing has since been switched on.
    assert await rig.outbox.claim_previewed((await rig.outbox.get_by_key(ARRIVAL_KEY)).id)

    await rig.service.tick(visit, location, names, index)

    assert rig.sender.calls == []
    assert await rig.visits.stock_posted(visit.activation) is True
    assert await _keys(db) == [ARRIVAL_KEY]


async def test_late_stock_transient_error_retries_next_tick(db, clock, real):
    visit, location, names, index = real
    rig = make_rig(db, clock)
    await rig.service.tick(replace(visit, manifest=()), location, names, index)
    await rig.outbox.mark_sent((await rig.outbox.get_by_key(ARRIVAL_KEY)).id, 77)
    rig.sender.edit_error = TransientPublishError("rate limited")

    await rig.service.tick(visit, location, names, index)
    assert len(rig.sender.edits()) == 1
    assert await rig.visits.stock_posted(visit.activation) is False

    rig.sender.edit_error = None
    await rig.service.tick(visit, location, names, index)
    assert len(rig.sender.edits()) == 2
    assert await rig.visits.stock_posted(visit.activation) is True


async def test_late_stock_permanent_error_gives_up(db, clock, real):
    visit, location, names, index = real
    big = _big(visit, index, names)
    rig = make_rig(db, clock)
    await rig.service.tick(replace(visit, manifest=()), location, names, index)
    await rig.outbox.mark_sent((await rig.outbox.get_by_key(ARRIVAL_KEY)).id, 77)
    rig.sender.edit_error = MessageMissing("deleted")

    await rig.service.tick(big, location, names, index)
    await rig.service.tick(big, location, names, index)

    assert len(rig.sender.edits()) == 1
    assert await rig.visits.stock_posted(visit.activation) is True
    # Without the first message, its follow-ups are not posted on their own.
    assert await _keys(db) == [ARRIVAL_KEY]


async def test_missing_baro_data_is_a_no_op(db, clock, real):
    _, _, _, index = real
    rig = make_rig(db, clock)

    assert await rig.service.tick(None, None, {}, index) == []

    assert await _keys(db) == []
    assert rig.sender.calls == []
    assert await rig.previews.count() == 0


@pytest.mark.parametrize(
    ("role_id", "categories", "expected"),
    [
        (4242, ["Baro Ki'Teer"], 4242),
        (4242, [], None),
        (4242, ["Hotfix"], None),
        (0, ["Baro Ki'Teer"], None),
    ],
)
async def test_role_ping_only_when_configured(db, clock, real, role_id, categories, expected):
    visit, location, names, index = real
    rig = make_rig(db, clock, notification_role_id=role_id, role_ping_categories=categories)

    await rig.service.tick(visit, location, names, index)

    row = await rig.outbox.get_by_key(ARRIVAL_KEY)
    assert row.payload["role_ping_id"] == expected


async def test_visit_repo_tracks_stock_per_visit(db, real):
    visit, _, _, _ = real
    visits = BaroVisitRepo(db)
    assert await visits.stock_posted(visit.activation) is False

    await visits.mark_stock_posted(visit.activation)
    await visits.mark_stock_posted(visit.activation)

    assert await visits.stock_posted(visit.activation) is True
    assert await visits.stock_posted(visit.expiry) is False
