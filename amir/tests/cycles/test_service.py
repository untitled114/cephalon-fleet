import asyncio
import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from amir.bot.client import CYCLES_ENSURE_SECONDS, CYCLES_TICK_SECONDS
from amir.config.settings import CyclesSettings
from amir.cycles.model import WORLD_NAMES, roll_forward
from amir.cycles.service import (
    RETRY_BASE_SECONDS,
    RETRY_MAX_SECONDS,
    REWARN_AFTER,
    CycleService,
)
from amir.publishing.cards import Card
from amir.publishing.gate import (
    ChannelKey,
    ChannelUnavailable,
    GatedWriter,
    MessageMissing,
    PermissionDenied,
    SendResult,
    TransientPublishError,
)
from amir.storage.db import Database
from amir.storage.repos import CycleCardRepo, PreviewRepo
from amir.worldstate.wfcd import parse_cycles

CAPTURE = "wfcd_pc_baro_present.json"
SETTINGS = CyclesSettings()


def at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 10, 25, hour, minute, second, tzinfo=UTC)


class RecordingSender:
    """A ChannelWriter that records calls and hands out fresh message ids.

    `edit_errors` maps a message id to the exception its edit raises; `send_errors` maps a card
    title to the exception sending it raises. Every attempt is recorded, a failed send with
    message id None. Every call yields to the event loop first, as a real Discord call would.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.edit_errors: dict[int, Exception] = {}
        self.send_errors: dict[str, Exception] = {}
        self._next_id = 9001

    async def send(
        self,
        channel: ChannelKey,
        *,
        cards: Sequence[Card] = (),
        text: str | None = None,
        role_ping_id: int | None = None,
    ) -> SendResult:
        await asyncio.sleep(0)
        for card in cards:
            if card.title in self.send_errors:
                self.calls.append(("send", channel, list(cards), None))
                raise self.send_errors[card.title]
        message_id = self._next_id
        self._next_id += 1
        self.calls.append(("send", channel, list(cards), message_id))
        return SendResult("sent", message_id)

    async def edit(
        self, channel: ChannelKey, message_id: int, *, cards: Sequence[Card]
    ) -> SendResult:
        await asyncio.sleep(0)
        self.calls.append(("edit", channel, message_id, list(cards)))
        error = self.edit_errors.get(message_id)
        if error is not None:
            raise error
        return SendResult("sent", message_id)

    async def find_recent(
        self, channel: ChannelKey, identities: set[tuple[str, str, str]], limit: int = 50
    ) -> int | None:
        return None

    def sends(self) -> list[tuple[Any, ...]]:
        """Successful sends only."""
        return [call for call in self.calls if call[0] == "send" and call[3] is not None]

    def attempts(self, title: str) -> int:
        """Sends and edits tried for the card with this title, failed ones included."""
        count = 0
        for call in self.calls:
            cards = call[3] if call[0] == "edit" else call[2]
            count += any(card.title == title for card in cards)
        return count

    def edits(self) -> list[tuple[Any, ...]]:
        return [call for call in self.calls if call[0] == "edit"]


@dataclass
class Rig:
    service: CycleService
    repo: CycleCardRepo
    previews: PreviewRepo
    sender: RecordingSender


class SlowSender(RecordingSender):
    """A RecordingSender whose edits of some messages take a while on the fake clock, as an edit
    does while discord.py retries a 5xx response itself. `edit_seconds` maps a message id to
    how long its edit takes."""

    def __init__(self, clock: Any) -> None:
        super().__init__()
        self.clock = clock
        self.edit_seconds: dict[int, float] = {}

    async def edit(
        self, channel: ChannelKey, message_id: int, *, cards: Sequence[Card]
    ) -> SendResult:
        self.clock.advance(self.edit_seconds.get(message_id, 0))
        return await super().edit(channel, message_id, cards=cards)


def make_rig(
    db: Database,
    clock: Any,
    *,
    enabled: bool = True,
    sender: RecordingSender | None = None,
    repo: CycleCardRepo | None = None,
) -> Rig:
    sender = sender or RecordingSender()
    previews = PreviewRepo(db, clock)
    repo = repo or CycleCardRepo(db)
    writer = GatedWriter(sender, previews, enabled=enabled)
    service = CycleService(
        writer, repo, worlds=SETTINGS.worlds, durations=SETTINGS.durations, clock=clock
    )
    return Rig(service, repo, previews, sender)


def advance_to(clock: Any, when: datetime) -> None:
    clock.advance((when - clock.now()).total_seconds())


async def tick_every_second(service: CycleService, clock: Any, seconds: int) -> None:
    for _ in range(seconds):
        await service.tick()
        clock.advance(1)


@pytest.fixture
def snaps(captured):
    return parse_cycles(captured(CAPTURE))


async def stored_ids(repo: CycleCardRepo) -> dict[str, int | None]:
    return {world: (await repo.get(world))[0] for world in WORLD_NAMES}


# --- CycleCardRepo ---------------------------------------------------------------------------


async def test_repo_round_trip(db, snaps):
    repo = CycleCardRepo(db)
    vallis = snaps["vallis"]
    assert await repo.get("vallis") == (None, None, None)

    await repo.save_state("vallis", vallis.state, vallis.expiry)
    assert await repo.get("vallis") == (None, "cold", vallis.expiry)

    await repo.save("vallis", 9001, vallis.state, vallis.expiry)
    assert await repo.get("vallis") == (9001, "cold", vallis.expiry)

    warm = roll_forward(vallis, at(1, 35), SETTINGS.durations)
    await repo.save_state("vallis", warm.state, warm.expiry)
    assert await repo.get("vallis") == (9001, "warm", at(1, 40, 8))
    assert await repo.get("cetus") == (None, None, None)


# --- CycleService ----------------------------------------------------------------------------


async def test_cards_posted_once_and_reused_after_restart(db, clock, snaps):
    rig = make_rig(db, clock)
    await rig.service.apply_snapshots(snaps)

    await rig.service.ensure_cards()

    sends = rig.sender.sends()
    assert len(sends) == 3
    assert [call[1] for call in sends] == [ChannelKey.CYCLES] * 3
    assert sorted(card.title for call in sends for card in call[2]) == sorted(WORLD_NAMES.values())
    ids = await stored_ids(rig.repo)
    assert sorted(ids.values()) == [9001, 9002, 9003]
    for world, snap in snaps.items():
        assert (await rig.repo.get(world))[1:] == (snap.state, snap.expiry)

    restarted = make_rig(db, clock)
    await restarted.service.apply_snapshots(snaps)
    await restarted.service.ensure_cards()
    await restarted.service.tick()

    assert restarted.sender.calls == []
    assert await stored_ids(restarted.repo) == ids


async def test_ensure_cards_skips_worlds_without_snapshot(db, clock, snaps):
    rig = make_rig(db, clock)
    await rig.service.apply_snapshots({"vallis": snaps["vallis"]})

    await rig.service.ensure_cards()

    assert [card.title for call in rig.sender.sends() for card in call[2]] == ["Orb Vallis"]
    assert (await rig.repo.get("cetus"))[0] is None


async def test_ensure_cards_failure_for_one_world_does_not_stop_others(db, clock, snaps, caplog):
    caplog.set_level(logging.INFO)
    rig = make_rig(db, clock)
    await rig.service.apply_snapshots(snaps)
    rig.sender.send_errors["Orb Vallis"] = ChannelUnavailable("cycles channel not visible")

    await rig.service.ensure_cards()

    titles = [card.title for call in rig.sender.sends() for card in call[2]]
    assert titles == ["Plains of Eidolon", "Cambion Drift"]
    assert await rig.repo.get("vallis") == (None, None, None)
    (warning,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "vallis" in warning.getMessage() and "ChannelUnavailable" in warning.getMessage()

    del rig.sender.send_errors["Orb Vallis"]
    await rig.service.ensure_cards()  # still backing off
    assert rig.sender.attempts("Orb Vallis") == 1

    clock.advance(RETRY_BASE_SECONDS)
    await rig.service.ensure_cards()

    titles = [card.title for call in rig.sender.sends() for card in call[2]]
    assert titles == ["Plains of Eidolon", "Cambion Drift", "Orb Vallis"]
    assert None not in (await stored_ids(rig.repo)).values()


async def test_only_changed_world_is_edited(db, clock, snaps):
    rig = make_rig(db, clock)
    await rig.service.apply_snapshots(snaps)
    await rig.service.ensure_cards()
    ids = await stored_ids(rig.repo)
    rig.sender.calls.clear()

    advance_to(clock, at(1, 35))
    await rig.service.tick()

    edits = rig.sender.edits()
    assert len(edits) == 1
    _, channel, message_id, cards = edits[0]
    assert channel is ChannelKey.CYCLES
    assert message_id == ids["vallis"]
    assert [card.title for card in cards] == ["Orb Vallis"]
    assert f"<t:{int(at(1, 40, 8).timestamp())}:R>" in cards[0].description
    assert rig.sender.sends() == []
    assert await rig.repo.get("vallis") == (ids["vallis"], "warm", at(1, 40, 8))

    await rig.service.tick()
    assert len(rig.sender.calls) == 1


async def test_missing_message_reposted(db, clock, snaps):
    rig = make_rig(db, clock)
    await rig.service.apply_snapshots(snaps)
    await rig.service.ensure_cards()
    old = (await rig.repo.get("vallis"))[0]
    assert old is not None
    rig.sender.calls.clear()
    rig.sender.edit_errors[old] = MessageMissing("deleted")

    advance_to(clock, at(1, 35))
    await rig.service.tick()

    assert [call[0] for call in rig.sender.calls] == ["edit", "send"]
    sent_cards = rig.sender.sends()[0][2]
    assert [card.title for card in sent_cards] == ["Orb Vallis"]
    new = rig.sender.sends()[0][3]
    assert new != old
    assert await rig.repo.get("vallis") == (new, "warm", at(1, 40, 8))

    await rig.service.tick()
    assert len(rig.sender.calls) == 2


async def test_one_world_failure_does_not_stop_others(db, clock, snaps, caplog):
    caplog.set_level(logging.INFO)
    rig = make_rig(db, clock)
    await rig.service.apply_snapshots(snaps)
    await rig.service.ensure_cards()
    ids = await stored_ids(rig.repo)
    rig.sender.calls.clear()
    rig.sender.edit_errors[ids["cetus"]] = TransientPublishError("rate limited")

    advance_to(clock, at(1, 43))
    await rig.service.tick()

    assert sorted(call[2] for call in rig.sender.edits()) == sorted(ids.values())
    assert await rig.repo.get("cetus") == (ids["cetus"], "night", at(1, 42))
    assert (await rig.repo.get("cambion"))[1] == "fass"
    assert (await rig.repo.get("vallis"))[1:] == ("cold", at(2, 0, 8))
    (warning,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "cetus" in warning.getMessage()
    assert "TransientPublishError" in warning.getMessage()
    assert not warning.exc_info

    del rig.sender.edit_errors[ids["cetus"]]
    rig.sender.calls.clear()
    await rig.service.tick()  # still backing off
    assert rig.sender.calls == []

    clock.advance(RETRY_BASE_SECONDS)
    await rig.service.tick()

    assert [call[2] for call in rig.sender.edits()] == [ids["cetus"]]
    assert await rig.repo.get("cetus") == (ids["cetus"], "day", at(3, 22))


class FailingStateRepo(CycleCardRepo):
    """A repo whose state write for one world fails, as a full disk would."""

    def __init__(self, db: Database, failing_world: str) -> None:
        super().__init__(db)
        self.failing_world: str | None = failing_world

    async def save_state(self, world, state, expiry):  # type: ignore[override]
        if world == self.failing_world:
            raise sqlite3.OperationalError("database or disk is full")
        await super().save_state(world, state, expiry)


async def test_storage_failure_for_one_world_retried_after_backoff(db, clock, snaps, caplog):
    caplog.set_level(logging.INFO)
    rig = make_rig(db, clock)
    await rig.service.apply_snapshots(snaps)
    await rig.service.ensure_cards()
    ids = await stored_ids(rig.repo)
    repo = FailingStateRepo(db, "cambion")
    writer = GatedWriter(rig.sender, rig.previews, enabled=True)
    service = CycleService(
        writer, repo, worlds=SETTINGS.worlds, durations=SETTINGS.durations, clock=clock
    )
    await service.apply_snapshots(snaps)
    rig.sender.calls.clear()

    advance_to(clock, at(1, 43))
    await service.tick()

    assert sorted(call[2] for call in rig.sender.edits()) == sorted(ids.values())
    assert await repo.get("cambion") == (ids["cambion"], "vome", at(1, 42))
    assert (await repo.get("cetus"))[1] == "day"
    (warning,) = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert "cambion" in warning.getMessage() and "OperationalError" in warning.getMessage()
    assert warning.exc_info and warning.exc_info[0] is sqlite3.OperationalError

    repo.failing_world = None
    rig.sender.calls.clear()
    clock.advance(RETRY_BASE_SECONDS)
    await service.tick()

    assert [call[2] for call in rig.sender.edits()] == [ids["cambion"]]
    assert await repo.get("cambion") == (ids["cambion"], "fass", at(3, 22))


async def test_enable_after_gated_run_posts_each_card_once(db, clock, snaps):
    gated = make_rig(db, clock, enabled=False)
    await gated.service.apply_snapshots(snaps)
    await gated.service.tick()
    first = await gated.previews.count()
    assert first == 3
    await gated.service.ensure_cards()
    assert await gated.previews.count() == first

    advance_to(clock, at(1, 35))  # vallis turns warm
    await gated.service.tick()
    advance_to(clock, at(1, 43))  # cetus and cambion flip, vallis is cold again
    await gated.service.tick()

    assert gated.sender.calls == []
    previews = await gated.previews.recent(50)
    assert len(previews) == 3 + 1 + 3
    assert {p["channel"] for p in previews} == {"cycles"}
    assert {p["action"] for p in previews} == {"send"}
    assert await stored_ids(gated.repo) == {world: None for world in WORLD_NAMES}
    assert await gated.repo.get("cetus") == (None, "day", at(3, 22))

    enabled = make_rig(db, clock, enabled=True)
    await enabled.service.apply_snapshots(snaps)
    await enabled.service.ensure_cards()

    sends = enabled.sender.sends()
    assert len(sends) == 3
    assert enabled.sender.calls == sends
    plains = next(card for call in sends for card in call[2] if card.title == "Plains of Eidolon")
    assert "**Day**" in plains.description
    assert f"<t:{int(at(3, 22).timestamp())}:R>" in plains.description
    ids = await stored_ids(enabled.repo)
    assert None not in ids.values()

    await enabled.service.ensure_cards()
    await enabled.service.tick()

    assert len(enabled.sender.calls) == 3
    assert await stored_ids(enabled.repo) == ids


async def test_enabled_tick_posts_missing_cards_whatever_the_call_order(db, clock, snaps):
    gated = make_rig(db, clock, enabled=False)
    await gated.service.apply_snapshots(snaps)
    await gated.service.tick()
    assert await gated.previews.count() == 3

    advance_to(clock, at(1, 25))  # restart with publishing on; no world has flipped since
    enabled = make_rig(db, clock, enabled=True)
    await enabled.service.ensure_cards()  # before any snapshot is held: nothing to post yet
    assert enabled.sender.calls == []
    await enabled.service.apply_snapshots(snaps)
    await enabled.service.tick()

    sends = enabled.sender.sends()
    assert len(sends) == 3
    assert enabled.sender.calls == sends
    plains = next(card for call in sends for card in call[2] if card.title == "Plains of Eidolon")
    assert "**Night**" in plains.description
    assert f"<t:{int(at(1, 42).timestamp())}:R>" in plains.description
    ids = await stored_ids(enabled.repo)
    assert None not in ids.values()

    await enabled.service.tick()
    await enabled.service.ensure_cards()

    assert len(enabled.sender.calls) == 3
    assert await enabled.repo.get("cetus") == (ids["cetus"], "night", at(1, 42))


class FailingSaveRepo(CycleCardRepo):
    """A repo that cannot store a new card's message id for one world, as a full disk would."""

    def __init__(self, db: Database, failing_world: str) -> None:
        super().__init__(db)
        self.failing_world: str | None = failing_world

    async def save(self, world, message_id, state, expiry):  # type: ignore[override]
        if world == self.failing_world:
            raise sqlite3.OperationalError("database or disk is full")
        await super().save(world, message_id, state, expiry)


async def test_id_kept_when_save_fails_after_send(db, clock, snaps, caplog):
    caplog.set_level(logging.INFO)
    sender = RecordingSender()
    repo = FailingSaveRepo(db, "vallis")
    service = CycleService(
        GatedWriter(sender, PreviewRepo(db, clock), enabled=True),
        repo,
        worlds=SETTINGS.worlds,
        durations=SETTINGS.durations,
        clock=clock,
    )
    await service.apply_snapshots(snaps)
    await service.ensure_cards()

    assert len(sender.sends()) == 3
    vallis_id = next(call[3] for call in sender.sends() if call[2][0].title == "Orb Vallis")
    (error,) = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert "vallis" in error.getMessage() and str(vallis_id) in error.getMessage()

    await tick_every_second(service, clock, 600)  # ten minutes with the disk still full

    assert len(sender.sends()) == 3
    assert sender.calls == sender.sends()
    assert await repo.get("vallis") == (None, None, None)

    repo.failing_world = None
    advance_to(clock, at(1, 35))  # vallis has turned warm meanwhile
    await service.tick()

    assert len(sender.sends()) == 3
    assert [call[2] for call in sender.edits()] == [vallis_id]
    assert await repo.get("vallis") == (vallis_id, "warm", at(1, 40, 8))


async def test_persistent_failure_backs_off_and_success_resets_it(db, clock, snaps, caplog):
    caplog.set_level(logging.DEBUG, logger="amir.cycles.service")
    rig = make_rig(db, clock)
    rig.sender.send_errors["Orb Vallis"] = PermissionDenied("missing Send Messages")
    await rig.service.apply_snapshots(snaps)

    await tick_every_second(rig.service, clock, 600)

    # Tried at 0, 5, 15, 35, 75, 155 and 315 s; the next try, capped, is due at 615 s.
    assert RETRY_BASE_SECONDS == 5 and RETRY_MAX_SECONDS == 300
    assert rig.sender.attempts("Orb Vallis") == 7
    assert rig.sender.attempts("Plains of Eidolon") == rig.sender.attempts("Cambion Drift") == 1
    failures = [r for r in caplog.records if "update failed world=vallis" in r.getMessage()]
    assert [r.levelno for r in failures] == [logging.WARNING] + [logging.DEBUG] * 6
    assert "PermissionDenied" in failures[0].getMessage()

    del rig.sender.send_errors["Orb Vallis"]
    await rig.service.tick()  # 600 s: still backing off
    assert rig.sender.attempts("Orb Vallis") == 7
    clock.advance(15)
    await rig.service.tick()  # 615 s: retried and posted

    assert rig.sender.attempts("Orb Vallis") == 8
    vallis_id = (await rig.repo.get("vallis"))[0]
    assert vallis_id is not None
    assert any("recovered world=vallis" in r.getMessage() for r in caplog.records)

    rig.sender.edit_errors[vallis_id] = PermissionDenied("missing Manage Messages")
    caplog.clear()
    advance_to(clock, at(1, 35))  # vallis turns warm: the edit fails
    await rig.service.tick()
    clock.advance(RETRY_BASE_SECONDS - 1)
    await rig.service.tick()
    assert rig.sender.attempts("Orb Vallis") == 9
    clock.advance(1)
    await rig.service.tick()  # the backoff restarted at 5 s

    assert rig.sender.attempts("Orb Vallis") == 10
    failures = [r for r in caplog.records if "update failed world=vallis" in r.getMessage()]
    assert [r.levelno for r in failures] == [logging.WARNING, logging.DEBUG]


async def test_failing_lists_each_failing_world_until_it_recovers(db, clock, snaps):
    rig = make_rig(db, clock)
    rig.sender.send_errors["Orb Vallis"] = PermissionDenied("missing Send Messages")
    await rig.service.apply_snapshots(snaps)
    assert rig.service.failing() == {}

    await rig.service.ensure_cards()

    retry_at = clock.now() + timedelta(seconds=RETRY_BASE_SECONDS)
    assert rig.service.failing() == {"vallis": ("PermissionDenied", retry_at)}

    rig.sender.send_errors["Orb Vallis"] = TransientPublishError("rate limited")
    advance_to(clock, retry_at)
    await rig.service.ensure_cards()  # failing again, now for another reason

    retry_at = clock.now() + timedelta(seconds=2 * RETRY_BASE_SECONDS)
    assert rig.service.failing() == {"vallis": ("TransientPublishError", retry_at)}

    del rig.sender.send_errors["Orb Vallis"]
    advance_to(clock, retry_at)
    await rig.service.ensure_cards()

    assert rig.service.failing() == {}
    assert None not in (await stored_ids(rig.repo)).values()


async def test_a_failure_streak_warns_again_at_most_hourly(db, clock, snaps, caplog):
    caplog.set_level(logging.DEBUG, logger="amir.cycles.service")
    rig = make_rig(db, clock)
    rig.sender.send_errors["Orb Vallis"] = PermissionDenied("missing Send Messages")
    await rig.service.apply_snapshots(snaps)
    start = clock.now()
    warned: list[float] = []
    levels: list[int] = []

    while clock.now() - start < timedelta(hours=2, minutes=30):
        caplog.clear()
        await rig.service.tick()
        (record,) = [r for r in caplog.records if "world=vallis" in r.getMessage()]
        levels.append(record.levelno)
        if record.levelno == logging.WARNING:
            warned.append((clock.now() - start).total_seconds())
        advance_to(clock, rig.service.failing()["vallis"][1])  # straight to the next retry

    # Retried at 0, 5, 15, 35, 75, 155 and 315 s, then every 300 s: the first retry at least
    # an hour after the last warning warns again, every other failure stays at DEBUG.
    assert REWARN_AFTER == timedelta(hours=1)
    assert warned == [0, 3615, 7215]
    assert set(levels) == {logging.WARNING, logging.DEBUG}
    assert rig.sender.attempts("Orb Vallis") == len(levels)


def vallis_records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if "world=vallis" in r.getMessage()]


async def test_ensure_finding_the_card_does_not_end_a_failure_streak(db, clock, snaps, caplog):
    caplog.set_level(logging.DEBUG, logger="amir.cycles.service")
    rig = make_rig(db, clock)
    await rig.service.apply_snapshots(snaps)
    await rig.service.ensure_cards()
    vallis_id = (await rig.repo.get("vallis"))[0]
    rig.sender.edit_errors[vallis_id] = TransientPublishError("503 Service Unavailable")
    advance_to(clock, at(1, 35))  # vallis turns warm: its edit fails from here on
    await rig.service.tick()
    streak = rig.service.failing()["vallis"]
    advance_to(clock, streak[1])
    caplog.clear()

    await rig.service.ensure_cards()  # every card exists: nothing to write

    assert rig.service.failing() == {"vallis": streak}
    assert vallis_records(caplog) == []  # above all, no "recovered"

    await rig.service.tick()  # the edit is retried and fails again: the same streak

    (record,) = vallis_records(caplog)
    assert record.levelno == logging.DEBUG
    assert "update failed world=vallis" in record.getMessage()
    retry_at = clock.now() + timedelta(seconds=2 * RETRY_BASE_SECONDS)
    assert rig.service.failing() == {"vallis": ("TransientPublishError", retry_at)}
    assert rig.sender.attempts("Orb Vallis") == 1 + 2  # the post, then two failed edits


async def test_the_backoff_runs_from_the_end_of_a_slow_failed_attempt(db, clock, snaps):
    sender = SlowSender(clock)
    rig = make_rig(db, clock, sender=sender)
    await rig.service.apply_snapshots(snaps)
    await rig.service.ensure_cards()
    vallis_id = (await rig.repo.get("vallis"))[0]
    sender.edit_errors[vallis_id] = TransientPublishError("503 Service Unavailable")
    sender.edit_seconds[vallis_id] = 25

    advance_to(clock, at(1, 35))
    await rig.service.tick()

    assert clock.now() == at(1, 35, 25)
    retry_at = at(1, 35, 25) + timedelta(seconds=RETRY_BASE_SECONDS)
    assert rig.service.failing() == {"vallis": ("TransientPublishError", retry_at)}


async def test_an_hour_of_slow_failing_edits_warns_once_and_never_recovers(
    db, clock, snaps, caplog
):
    """The bot's two loops for an hour: a tick every second and ensure_cards every 30 s, while
    every edit of Orb Vallis's card fails after 25 s of discord.py retrying a 5xx itself."""
    caplog.set_level(logging.DEBUG, logger="amir.cycles.service")
    sender = SlowSender(clock)
    rig = make_rig(db, clock, sender=sender)
    await rig.service.apply_snapshots(snaps)
    await rig.service.ensure_cards()
    vallis_id = (await rig.repo.get("vallis"))[0]
    sender.edit_errors[vallis_id] = TransientPublishError("503 Service Unavailable")
    sender.edit_seconds[vallis_id] = 25
    advance_to(clock, at(1, 35))
    start = next_tick = clock.now()
    next_ensure = start + timedelta(seconds=0.5)
    listed: list[bool] = []
    caplog.clear()

    while clock.now() - start < timedelta(hours=1):
        if next_tick <= next_ensure:
            advance_to(clock, max(clock.now(), next_tick))
            await rig.service.tick()
            next_tick = clock.now() + timedelta(seconds=CYCLES_TICK_SECONDS)
        else:
            advance_to(clock, max(clock.now(), next_ensure))
            await rig.service.ensure_cards()
            next_ensure = clock.now() + timedelta(seconds=CYCLES_ENSURE_SECONDS)
        listed.append("vallis" in rig.service.failing())

    records = vallis_records(caplog)
    warnings = [r for r in records if r.levelno == logging.WARNING]
    assert 1 <= len(warnings) <= 2
    assert not any("recovered" in r.getMessage() for r in records)
    assert all(listed)
    assert rig.sender.attempts("Orb Vallis") > 10  # it kept trying all the while


class FlakyReadRepo(CycleCardRepo):
    """A repo whose next read for `fail_next` fails once, as a locked database would."""

    def __init__(self, db: Database) -> None:
        super().__init__(db)
        self.fail_next: str | None = None

    async def get(self, world):  # type: ignore[override]
        if world == self.fail_next:
            self.fail_next = None
            raise sqlite3.OperationalError("database is locked")
        return await super().get(world)


async def test_a_failed_read_clears_on_the_next_clean_tick(db, clock, snaps, caplog):
    # A tick that finds the card already showing the current state ends the streak: a failed
    # send or edit always leaves the stored state stale, so no write can be pending.
    caplog.set_level(logging.DEBUG, logger="amir.cycles.service")
    repo = FlakyReadRepo(db)
    rig = make_rig(db, clock, repo=repo)
    await rig.service.apply_snapshots(snaps)
    await rig.service.ensure_cards()
    rig.sender.calls.clear()
    caplog.clear()
    repo.fail_next = "cetus"

    await rig.service.tick()  # cetus's read fails; no world has changed since the posts

    retry_at = clock.now() + timedelta(seconds=RETRY_BASE_SECONDS)
    assert rig.service.failing() == {"cetus": ("OperationalError", retry_at)}

    advance_to(clock, retry_at)
    await rig.service.tick()  # a clean read: the card already shows the current state

    assert rig.service.failing() == {}
    for _ in range(10):  # five minutes of the bot's loops, still before cetus changes at 01:42
        await rig.service.ensure_cards()
        await tick_every_second(rig.service, clock, 30)
        assert rig.service.failing() == {}
    cetus = [r for r in caplog.records if "world=cetus" in r.getMessage()]
    assert [(r.levelno, r.getMessage()) for r in cetus] == [
        (
            logging.WARNING,
            "cycle card update failed world=cetus error=OperationalError;"
            f" retrying in {RETRY_BASE_SECONDS}s",
        ),
        (logging.INFO, "cycle card recovered world=cetus"),
    ]
    assert rig.sender.calls == []  # nothing needed writing


async def test_storing_a_kept_card_id_ends_the_failure_streak(db, clock, snaps, caplog):
    # Storing the id completes a post that succeeded: a write, though not a Discord call.
    caplog.set_level(logging.INFO, logger="amir.cycles.service")
    sender = RecordingSender()
    repo = FailingSaveRepo(db, "vallis")
    service = CycleService(
        GatedWriter(sender, PreviewRepo(db, clock), enabled=True),
        repo,
        worlds=SETTINGS.worlds,
        durations=SETTINGS.durations,
        clock=clock,
    )
    await service.apply_snapshots(snaps)
    await service.ensure_cards()
    error, retry_at = service.failing()["vallis"]
    assert error == "OperationalError"
    vallis_id = next(call[3] for call in sender.sends() if call[2][0].title == "Orb Vallis")

    repo.failing_world = None
    advance_to(clock, retry_at)  # vallis has not changed: storing its id is all there is to do
    await service.tick()

    assert service.failing() == {}
    assert await repo.get("vallis") == (vallis_id, "cold", snaps["vallis"].expiry)
    assert any("recovered world=vallis" in r.getMessage() for r in caplog.records)
    assert len(sender.calls) == 3  # the three posts; nothing posted or edited since


async def test_concurrent_tick_and_ensure_post_each_card_once(db, clock, snaps):
    rig = make_rig(db, clock)
    await rig.service.apply_snapshots(snaps)

    await asyncio.gather(rig.service.tick(), rig.service.ensure_cards())

    assert len(rig.sender.calls) == 3
    assert len(rig.sender.sends()) == 3


async def test_stale_snapshot_ignored(db, clock, snaps):
    rig = make_rig(db, clock)
    await rig.service.apply_snapshots(snaps)
    corrected = replace(snaps["vallis"], expiry=snaps["vallis"].expiry + timedelta(seconds=30))
    await rig.service.apply_snapshots({"vallis": corrected})
    assert rig.service.next_change_at() == at(1, 33, 58)

    await rig.service.apply_snapshots(snaps)  # an older response: vallis expiry is earlier

    assert rig.service.next_change_at() == at(1, 33, 58)
    await rig.service.ensure_cards()
    vallis_card = next(c for call in rig.sender.sends() for c in call[2] if c.title == "Orb Vallis")
    assert f"<t:{int(at(1, 33, 58).timestamp())}:R>" in vallis_card.description
    assert (await rig.repo.get("vallis"))[1:] == ("cold", at(1, 33, 58))


async def test_stale_after_flip_ignored(db, clock, snaps):
    rig = make_rig(db, clock)
    await rig.service.apply_snapshots(snaps)
    advance_to(clock, at(1, 35))
    await rig.service.tick()

    sent = list(rig.sender.calls)

    await rig.service.apply_snapshots(snaps)  # a cached response from before the flip
    await rig.service.tick()

    assert rig.sender.calls == sent
    assert (await rig.repo.get("vallis"))[1:] == ("warm", at(1, 40, 8))
    assert rig.service.next_change_at() == at(1, 40, 8)


async def test_unknown_state_snapshot_skipped(db, clock, snaps, caplog):
    caplog.set_level(logging.INFO)
    rig = make_rig(db, clock)
    odd = replace(snaps["vallis"], state=snaps["vallis"].state.upper())

    await rig.service.apply_snapshots({**snaps, "vallis": odd})

    assert rig.service.next_change_at() == at(1, 42)
    await rig.service.ensure_cards()
    assert sorted(card.title for call in rig.sender.sends() for card in call[2]) == [
        "Cambion Drift",
        "Plains of Eidolon",
    ]
    assert any("vallis" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


async def test_next_change_at_is_soonest(db, clock, snaps):
    rig = make_rig(db, clock)
    assert rig.service.next_change_at() is None

    await rig.service.apply_snapshots(snaps)

    assert clock.now() == at(1, 21)
    assert rig.service.next_change_at() == at(1, 33, 28)
    advance_to(clock, at(1, 35))
    assert rig.service.next_change_at() == at(1, 40, 8)
    advance_to(clock, at(1, 41))
    assert rig.service.next_change_at() == at(1, 42)


async def test_current_rolls_every_held_world(db, clock, snaps):
    rig = make_rig(db, clock)
    assert rig.service.current() == []

    await rig.service.apply_snapshots(snaps)
    advance_to(clock, at(1, 35))

    expected = [roll_forward(snaps[w], clock.now(), SETTINGS.durations) for w in SETTINGS.worlds]
    assert rig.service.current() == expected
    assert min(s.expiry for s in expected) == rig.service.next_change_at()
    assert rig.sender.calls == []  # reading the state never writes
