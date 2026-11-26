import asyncio
import json
import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from amir.baro.service import BaroService
from amir.config.settings import BaroSettings, CyclesSettings, WorldStateSettings
from amir.cycles.service import CycleService
from amir.net.http import FetchError, FetchResult
from amir.publishing.cards import Card
from amir.publishing.gate import ChannelKey, GatedWriter, SendResult
from amir.storage.db import Database
from amir.storage.repos import BaroVisitRepo, CycleCardRepo, ItemNameRepo, OutboxRepo, PreviewRepo
from amir.worldstate import poller as poller_module
from amir.worldstate.official import parse_void_trader
from amir.worldstate.poller import PollSummary, WorldStatePoller
from amir.worldstate.wfcd import parse_cycles, parse_item_names

OFFICIAL = "official_worldstate_baro_present.json"
WFCD = "wfcd_pc_baro_present.json"
ITEMS = "wfcd_items_names.json"

SETTINGS = WorldStateSettings()
CYCLES = CyclesSettings()


def at(day: int, hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 10, day, hour, minute, second, tzinfo=UTC)


class FakeFetcher:
    """HttpFetcher stand-in: url -> FetchResult or exception; a list plays back in order."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = dict(responses)
        self.calls: list[str] = []

    async def fetch(
        self, url: str, *, etag: str | None = None, last_modified: str | None = None
    ) -> FetchResult:
        self.calls.append(url)
        response = self.responses[url]
        if isinstance(response, list):
            response = response.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    def count(self, url: str) -> int:
        return self.calls.count(url)


class StalledItemsFetcher(FakeFetcher):
    """The item database never answers, as in the 2026-10-26 capture run."""

    def __init__(self, responses: dict[str, Any]) -> None:
        super().__init__(responses)
        self.items_started = asyncio.Event()

    async def fetch(self, url: str, **kwargs: Any) -> FetchResult:
        if url == SETTINGS.items_url:
            self.calls.append(url)
            self.items_started.set()
            await asyncio.Event().wait()
        return await super().fetch(url, **kwargs)


class HungOfficialFetcher(FakeFetcher):
    """The official world state never answers; WFCD and the item database do."""

    def __init__(self, responses: dict[str, Any]) -> None:
        super().__init__(responses)
        self.official_cancelled = False

    async def fetch(self, url: str, **kwargs: Any) -> FetchResult:
        if url == SETTINGS.official_url:
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                self.official_cancelled = True
                raise
        return await super().fetch(url, **kwargs)


class SignallingCycles(CycleService):
    """The real cycle service, announcing each finished tick."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.ticked = asyncio.Event()

    async def tick(self) -> None:
        await super().tick()
        self.ticked.set()


class CountingNames(ItemNameRepo):
    """The real repo, counting full reads of the cache."""

    def __init__(self, db: Database) -> None:
        super().__init__(db)
        self.reads = 0

    async def all(self) -> dict[str, str]:
        self.reads += 1
        return await super().all()


def ok(body: bytes) -> FetchResult:
    return FetchResult(200, body, None, None, False)


class NeverSender:
    """The Discord side of the gate. Publishing is off in these tests, so nothing reaches it."""

    async def send(
        self,
        channel: ChannelKey,
        *,
        cards: Sequence[Card] = (),
        text: str | None = None,
        role_ping_id: int | None = None,
    ) -> SendResult:
        raise AssertionError("publishing is disabled")

    async def edit(
        self, channel: ChannelKey, message_id: int, *, cards: Sequence[Card]
    ) -> SendResult:
        raise AssertionError("publishing is disabled")

    async def find_recent(
        self, channel: ChannelKey, identities: set[tuple[str, str, str]], limit: int = 50
    ) -> int | None:
        raise AssertionError("publishing is disabled")


@dataclass
class Rig:
    poller: WorldStatePoller
    fetcher: FakeFetcher
    names: ItemNameRepo
    outbox: OutboxRepo
    previews: PreviewRepo
    cards: CycleCardRepo
    cycles: CycleService


def make_rig(
    db: Database,
    clock: Any,
    responses: dict[str, Any],
    *,
    settings: WorldStateSettings = SETTINGS,
    fetcher: FakeFetcher | None = None,
    outbox: OutboxRepo | None = None,
    names: ItemNameRepo | None = None,
    cycles_class: type[CycleService] = CycleService,
    poller_class: type[WorldStatePoller] = WorldStatePoller,
    **kwargs: Any,
) -> Rig:
    """Real Baro and cycle services behind a disabled gate, fed by a fake fetcher."""
    previews = PreviewRepo(db, clock)
    outbox = outbox or OutboxRepo(db, clock)
    writer = GatedWriter(NeverSender(), previews, enabled=False)
    baro = BaroService(outbox, BaroVisitRepo(db), writer, BaroSettings(), clock)
    cards = CycleCardRepo(db)
    cycles = cycles_class(
        writer, cards, worlds=CYCLES.worlds, durations=CYCLES.durations, clock=clock
    )
    names = names or ItemNameRepo(db)
    fetcher = fetcher or FakeFetcher(responses)
    poller = poller_class(fetcher, settings, baro, cycles, names, clock, **kwargs)
    return Rig(poller, fetcher, names, outbox, previews, cards, cycles)


@pytest.fixture
def sources(captured) -> dict[str, FetchResult]:
    """Every world-state URL answering with its real 2026-10-24 capture."""
    return {
        SETTINGS.official_url: ok(captured(OFFICIAL)),
        SETTINGS.wfcd_url: ok(captured(WFCD)),
        SETTINGS.items_url: ok(captured(ITEMS)),
    }


@pytest.fixture
def arrival_key(captured) -> str:
    visit = parse_void_trader(captured(OFFICIAL))
    return f"baro:{int(visit.activation.timestamp())}:arrival"


@pytest.fixture
def real_names(captured) -> dict[str, str]:
    return parse_item_names(captured(ITEMS))


async def outbox_keys(db: Database) -> list[str]:
    async with db.conn.execute("SELECT dedup_key FROM outbox ORDER BY id") as cur:
        return [row[0] for row in await cur.fetchall()]


async def card_text(outbox: OutboxRepo, key: str) -> str:
    row = await outbox.get_by_key(key)
    assert row is not None
    cards = [Card.from_dict(card) for card in row.payload["cards"]]
    return "\n".join(card.title + "\n" + card.description for card in cards)


async def shown(cards: CycleCardRepo) -> dict[str, tuple[str | None, datetime | None]]:
    """What each world's card shows (or would show), from the cycle card table."""
    return {world: (await cards.get(world))[1:] for world in CYCLES.worlds}


def captured_cycles(captured) -> dict[str, tuple[str, datetime]]:
    return {w: (s.state, s.expiry) for w, s in parse_cycles(captured(WFCD)).items()}


# --- ItemNameRepo ----------------------------------------------------------------------------


async def test_item_name_repo_empty(db):
    repo = ItemNameRepo(db)

    assert await repo.all() == {}
    assert await repo.fetched_at() is None


async def test_item_name_repo_round_trip(db, real_names):
    repo = ItemNameRepo(db)

    await repo.replace_all(real_names, at(25, 1, 21))

    assert await repo.all() == real_names
    assert len(real_names) == 17934
    assert await repo.fetched_at() == at(25, 1, 21)


async def test_item_name_repo_replaces_rather_than_merges(db, real_names):
    repo = ItemNameRepo(db)
    await repo.replace_all(real_names, at(25, 1, 21))
    first_ten = dict(list(real_names.items())[:10])

    await repo.replace_all(first_ten, at(26, 1, 21))

    assert await repo.all() == first_ten
    assert await repo.fetched_at() == at(26, 1, 21)


async def test_item_name_repo_failed_replace_keeps_old_names(db, real_names):
    repo = ItemNameRepo(db)
    await repo.replace_all(real_names, at(25, 1, 21))
    broken = dict(real_names)
    broken[next(reversed(broken))] = None  # violates NOT NULL on the last insert

    with pytest.raises(sqlite3.IntegrityError):
        await repo.replace_all(broken, at(26, 1, 21))  # type: ignore[arg-type]

    assert await repo.all() == real_names
    assert await repo.fetched_at() == at(25, 1, 21)


async def test_item_name_repo_rejects_naive_fetched_at(db, real_names):
    repo = ItemNameRepo(db)
    await repo.replace_all(real_names, at(25, 1, 21))

    with pytest.raises(ValueError):
        await repo.replace_all({}, datetime(2026, 10, 26, 1, 21))

    assert await repo.all() == real_names


# --- WorldStatePoller ------------------------------------------------------------------------


async def test_both_sources_feed_services(db, clock, captured, sources, arrival_key):
    rig = make_rig(db, clock, sources)
    assert clock.now() == at(25, 1, 21)

    summary = await rig.poller.poll_once()

    assert summary == PollSummary(official_ok=True, wfcd_ok=True, items_refreshed=True, errors=[])
    # The cycle service holds all three captured snapshots: each card shows its world's state.
    assert await shown(rig.cards) == captured_cycles(captured)
    assert rig.cycles.next_change_at() == at(25, 1, 33, 28)
    previews = await rig.previews.recent(10)
    assert [(p["channel"], p["action"]) for p in previews] == [("cycles", "send")] * 3
    # Baro is present at 01:21Z, so the arrival row is queued, named from WFCD's location.
    assert await outbox_keys(db) == [arrival_key]
    assert "Strata Relay (Earth)" in await card_text(rig.outbox, arrival_key)
    assert sorted(rig.fetcher.calls) == sorted(
        [SETTINGS.official_url, SETTINGS.wfcd_url, SETTINGS.items_url]
    )


async def test_official_and_wfcd_fetched_concurrently(db, clock, sources):
    together = {SETTINGS.official_url, SETTINGS.wfcd_url}

    class RendezvousFetcher(FakeFetcher):
        """Holds the official and WFCD fetches until both have started."""

        def __init__(self, responses: dict[str, Any]) -> None:
            super().__init__(responses)
            self.waiting = set(together)
            self.both_started = asyncio.Event()

        async def fetch(self, url: str, **kwargs: Any) -> FetchResult:
            if url in self.waiting:
                self.waiting.discard(url)
                if not self.waiting:
                    self.both_started.set()
                await self.both_started.wait()
            return await super().fetch(url, **kwargs)

    rig = make_rig(db, clock, {}, fetcher=RendezvousFetcher(sources))

    # Fetching one after the other would wait forever on the first.
    summary = await asyncio.wait_for(rig.poller.poll_once(), timeout=5)

    assert summary.official_ok and summary.wfcd_ok


async def test_official_down_cycles_still_update(db, clock, captured, sources):
    sources[SETTINGS.official_url] = FetchError("http", 503)
    rig = make_rig(db, clock, sources)

    summary = await rig.poller.poll_once()

    assert summary.official_ok is False
    assert summary.wfcd_ok is True
    assert summary.items_refreshed is True
    assert len(summary.errors) == 1
    assert summary.errors[0].startswith("official: FetchError")
    assert "503" in summary.errors[0]
    assert await shown(rig.cards) == captured_cycles(captured)
    assert rig.cycles.next_change_at() == at(25, 1, 33, 28)
    assert await outbox_keys(db) == []


async def test_wfcd_down_baro_still_ticks(db, clock, sources, real_names, arrival_key):
    # The cache is a day and an hour old, so this poll also tries (and fails) to refresh it.
    cached_at = clock.now() - timedelta(hours=25)
    rig = make_rig(db, clock, sources)
    await rig.names.replace_all(real_names, cached_at)
    rig.fetcher.responses[SETTINGS.wfcd_url] = FetchError("network")
    rig.fetcher.responses[SETTINGS.items_url] = FetchError("timeout")

    summary = await rig.poller.poll_once()

    assert summary.official_ok is True
    assert summary.wfcd_ok is False
    assert summary.items_refreshed is False
    assert [error.split(":")[0] for error in summary.errors] == ["wfcd", "items"]
    assert await outbox_keys(db) == [arrival_key]
    text = await card_text(rig.outbox, arrival_key)
    # No WFCD location or inventory names: the relay comes from the node, the stock from cache.
    assert "Earth relay" in text
    assert "Peculiar End" in text
    assert await rig.names.all() == real_names
    assert await rig.names.fetched_at() == cached_at
    assert rig.cycles.next_change_at() is None


async def test_item_names_cached_24h(db, clock, sources):
    rig = make_rig(db, clock, sources)
    items = SETTINGS.items_url
    first = clock.now()

    assert (await rig.poller.poll_once()).items_refreshed is True
    assert rig.fetcher.count(items) == 1
    assert await rig.names.fetched_at() == first

    clock.advance(3600)
    assert (await rig.poller.poll_once()).items_refreshed is False
    assert rig.fetcher.count(items) == 1

    clock.advance(24 * 3600)
    assert (await rig.poller.poll_once()).items_refreshed is True
    assert rig.fetcher.count(items) == 2
    assert await rig.names.fetched_at() == first + timedelta(hours=25)


async def test_items_failure_backs_off_an_hour(db, clock, sources, real_names):
    cached_at = clock.now() - timedelta(hours=25)
    rig = make_rig(db, clock, sources)
    await rig.names.replace_all(real_names, cached_at)
    rig.fetcher.responses[SETTINGS.items_url] = [
        FetchError("timeout"),
        sources[SETTINGS.items_url],
    ]
    failed_at = clock.now()

    failed = await rig.poller.poll_once()

    assert failed.items_refreshed is False
    assert failed.official_ok and failed.wfcd_ok
    assert len(failed.errors) == 1
    assert failed.errors[0].startswith("items: FetchError")
    assert await rig.names.all() == real_names
    assert await rig.names.fetched_at() == cached_at

    clock.advance(10 * 60)
    assert await rig.poller.poll_once() == PollSummary(True, True, False, [])
    assert rig.fetcher.count(SETTINGS.items_url) == 1

    clock.advance((failed_at + timedelta(minutes=61) - clock.now()).total_seconds())
    assert await rig.poller.poll_once() == PollSummary(True, True, True, [])
    assert rig.fetcher.count(SETTINGS.items_url) == 2
    assert await rig.names.fetched_at() == clock.now()


async def test_first_poll_resolves_real_names(db, clock, sources, arrival_key):
    rig = make_rig(db, clock, sources)
    assert await rig.names.fetched_at() is None
    assert clock.now() == at(25, 1, 21)

    summary = await rig.poller.poll_once()

    assert summary == PollSummary(True, True, True, [])
    text = await card_text(rig.outbox, arrival_key)
    assert "Mark Of The Beast" in text
    assert "Weapon Glaive On Six Kills Buff Secondary" not in text


async def test_first_fill_failure_still_ticks_baro_and_retries_next_poll(
    db, clock, sources, arrival_key
):
    rig = make_rig(db, clock, sources)
    rig.fetcher.responses[SETTINGS.items_url] = [
        FetchError("timeout"),
        sources[SETTINGS.items_url],
    ]

    failed = await rig.poller.poll_once()

    assert failed.items_refreshed is False
    assert len(failed.errors) == 1
    assert failed.errors[0].startswith("items: FetchError")
    # Baro still ticks, with the names WFCD's own Baro inventory gives.
    assert await outbox_keys(db) == [arrival_key]
    assert "Prime Fulmination" in await card_text(rig.outbox, arrival_key)

    clock.advance(SETTINGS.interval_seconds)  # never filled: no backoff
    assert await rig.poller.poll_once() == PollSummary(True, True, True, [])
    assert rig.fetcher.count(SETTINGS.items_url) == 2
    assert len(await rig.names.all()) == 17934


async def test_slow_item_database_never_delays_cycles_or_baro(
    db, clock, captured, sources, real_names, arrival_key
):
    fetcher = StalledItemsFetcher(sources)
    rig = make_rig(db, clock, {}, fetcher=fetcher)
    await rig.names.replace_all(real_names, clock.now() - timedelta(hours=25))

    poll = asyncio.create_task(rig.poller.poll_once())
    await asyncio.wait_for(fetcher.items_started.wait(), timeout=5)

    # While the item database is still loading, cycles and Baro are already done.
    assert await shown(rig.cards) == captured_cycles(captured)
    assert await outbox_keys(db) == [arrival_key]
    poll.cancel()
    with pytest.raises(asyncio.CancelledError):
        await poll


async def test_first_fill_waits_before_baro_but_not_before_cycles(db, clock, captured, sources):
    fetcher = StalledItemsFetcher(sources)
    rig = make_rig(db, clock, {}, fetcher=fetcher)

    poll = asyncio.create_task(rig.poller.poll_once())
    await asyncio.wait_for(fetcher.items_started.wait(), timeout=5)

    # The cache was never filled: cycles are done, Baro waits for the real names.
    assert await shown(rig.cards) == captured_cycles(captured)
    assert await outbox_keys(db) == []
    poll.cancel()
    with pytest.raises(asyncio.CancelledError):
        await poll


async def test_names_cached_before_arrival_resolve_its_stock(
    db, make_clock, sources, captured, arrival_key
):
    visit = parse_void_trader(captured(OFFICIAL))
    clock = make_clock(visit.activation - timedelta(minutes=1))
    names = CountingNames(db)
    rig = make_rig(db, clock, sources, names=names)

    before = await rig.poller.poll_once()  # 72 h warning window: no stock yet; names fetched
    clock.advance(120)
    after = await rig.poller.poll_once()  # arrival

    assert before.items_refreshed is True
    assert after.items_refreshed is False
    assert rig.fetcher.count(SETTINGS.items_url) == 1
    assert "Peculiar End" in await card_text(rig.outbox, arrival_key)
    # The index came straight from the refresh; the cache was never read back.
    assert names.reads == 0


async def test_index_built_from_cache_once(db, clock, sources, real_names, arrival_key):
    names = CountingNames(db)
    rig = make_rig(db, clock, sources, names=names)
    await names.replace_all(real_names, clock.now() - timedelta(hours=1))

    await rig.poller.poll_once()
    clock.advance(SETTINGS.interval_seconds)
    await rig.poller.poll_once()

    assert names.reads == 1
    assert rig.fetcher.count(SETTINGS.items_url) == 0
    assert "Peculiar End" in await card_text(rig.outbox, arrival_key)


async def test_unreadable_item_cache_skips_refresh_only(db, clock, captured, sources, arrival_key):
    class BrokenStamp(ItemNameRepo):
        async def fetched_at(self) -> datetime | None:
            raise sqlite3.OperationalError("disk I/O error")

    rig = make_rig(db, clock, sources, names=BrokenStamp(db))

    summary = await rig.poller.poll_once()

    assert summary.items_refreshed is False
    assert len(summary.errors) == 1
    assert summary.errors[0].startswith("items: OperationalError")
    assert rig.fetcher.count(SETTINGS.items_url) == 0
    assert await shown(rig.cards) == captured_cycles(captured)
    assert await outbox_keys(db) == [arrival_key]


def truncated(body: bytes) -> bytes:
    return body[: len(body) // 2]


def without_void_traders(body: bytes) -> bytes:
    doc = json.loads(body)
    del doc["VoidTraders"]
    return json.dumps(doc).encode()


def infinite_price(body: bytes) -> bytes:
    doc = json.loads(body)
    doc["VoidTraders"][0]["Manifest"][0]["PrimePrice"] = float("inf")  # serialised as Infinity
    return json.dumps(doc).encode()


def deeply_nested(body: bytes) -> bytes:
    return b"[" * 200_000 + body + b"]" * 200_000


@pytest.mark.parametrize(
    ("derive", "error"),
    [
        (truncated, "WorldStateParseError"),
        (without_void_traders, "WorldStateParseError"),
        (infinite_price, "OverflowError"),
        (deeply_nested, "RecursionError"),
    ],
    ids=["truncated", "no VoidTraders", "infinite price", "deeply nested"],
)
async def test_unusable_official_body_is_recorded_not_fatal(
    db, clock, captured, sources, derive, error
):
    sources[SETTINGS.official_url] = ok(derive(captured(OFFICIAL)))
    rig = make_rig(db, clock, sources)

    summary = await rig.poller.poll_once()

    assert summary.official_ok is False
    assert summary.wfcd_ok is True
    assert len(summary.errors) == 1
    assert summary.errors[0].startswith(f"official: {error}")
    assert await outbox_keys(db) == []
    assert await shown(rig.cards) == captured_cycles(captured)


async def test_wfcd_inventory_type_error_is_recorded_not_fatal(
    db, clock, captured, sources, arrival_key
):
    doc = json.loads(captured(WFCD))
    entry = doc["voidTrader"]["inventory"][0]
    entry["uniqueName"] = [entry["uniqueName"]]  # a list cannot be a dict key
    sources[SETTINGS.wfcd_url] = ok(json.dumps(doc).encode())
    rig = make_rig(db, clock, sources)

    summary = await rig.poller.poll_once()

    assert summary.wfcd_ok is False
    assert len(summary.errors) == 1
    assert summary.errors[0].startswith("wfcd baro: TypeError")
    assert await shown(rig.cards) == captured_cycles(captured)
    assert "Earth relay" in await card_text(rig.outbox, arrival_key)


async def test_hung_official_never_holds_back_cycles(db, clock, captured, sources, monkeypatch):
    monkeypatch.setattr(poller_module, "OFFICIAL_DEADLINE", 1.0)
    fetcher = HungOfficialFetcher(sources)
    rig = make_rig(db, clock, {}, fetcher=fetcher, cycles_class=SignallingCycles)
    loop = asyncio.get_running_loop()
    started = loop.time()

    poll = asyncio.create_task(rig.poller.poll_once())
    async with asyncio.timeout(5):
        await rig.cycles.ticked.wait()  # type: ignore[attr-defined]

    # The cycle cards are updated while the official fetch is still waiting.
    assert await shown(rig.cards) == captured_cycles(captured)
    assert not fetcher.official_cancelled
    assert not poll.done()

    summary = await asyncio.wait_for(poll, timeout=5)

    assert fetcher.official_cancelled
    assert 1.0 <= loop.time() - started < 3.0
    assert summary.official_ok is False
    assert summary.wfcd_ok is True
    assert summary.errors == ["official: TimeoutError: no answer within 1 s"]
    assert await outbox_keys(db) == []


async def test_hung_item_database_is_cut_off_at_its_deadline(
    db, clock, sources, real_names, arrival_key, monkeypatch
):
    monkeypatch.setattr(poller_module, "ITEMS_DEADLINE", 0.2)
    rig = make_rig(db, clock, {}, fetcher=StalledItemsFetcher(sources))
    cached_at = clock.now() - timedelta(hours=25)
    await rig.names.replace_all(real_names, cached_at)
    loop = asyncio.get_running_loop()
    started = loop.time()

    summary = await asyncio.wait_for(rig.poller.poll_once(), timeout=5)

    assert 0.2 <= loop.time() - started < 2.0
    assert summary == PollSummary(
        True, True, False, ["items: TimeoutError: no answer within 0.2 s"]
    )
    assert await rig.names.fetched_at() == cached_at
    assert await outbox_keys(db) == [arrival_key]


async def test_hung_item_database_keeps_polls_on_schedule(
    db, clock, sources, real_names, monkeypatch
):
    monkeypatch.setattr(poller_module, "ITEMS_DEADLINE", 0.2)
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise asyncio.CancelledError
        clock.advance(seconds)

    fetcher = StalledItemsFetcher(sources)
    settings = WorldStateSettings(interval_seconds=3600)
    rig = make_rig(db, clock, {}, fetcher=fetcher, settings=settings, sleep=sleep)
    await rig.names.replace_all(real_names, clock.now() - timedelta(hours=25))

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(rig.poller.run(), timeout=5)

    # 5 s past the vallis flip at 01:33:28, then 5 s past its next one at 01:40:08. The second
    # poll leaves the item database alone: it failed under an hour ago.
    assert sleeps == [753, 400]
    assert fetcher.count(SETTINGS.wfcd_url) == 2
    assert fetcher.count(SETTINGS.items_url) == 1


async def test_first_fill_deadline_then_baro_ticks_with_wfcd_names(
    db, clock, sources, arrival_key, monkeypatch
):
    monkeypatch.setattr(poller_module, "ITEMS_DEADLINE", 0.2)
    rig = make_rig(db, clock, {}, fetcher=StalledItemsFetcher(sources))

    summary = await asyncio.wait_for(rig.poller.poll_once(), timeout=5)

    assert summary.errors == ["items: TimeoutError: no answer within 0.2 s"]
    assert "Prime Fulmination" in await card_text(rig.outbox, arrival_key)


async def test_wfcd_without_a_cycle_still_names_baro_location(
    db, clock, captured, sources, arrival_key
):
    doc = json.loads(captured(WFCD))
    del doc["vallisCycle"]
    sources[SETTINGS.wfcd_url] = ok(json.dumps(doc).encode())
    rig = make_rig(db, clock, sources)

    summary = await rig.poller.poll_once()

    assert summary.wfcd_ok is False
    assert summary.official_ok is True
    assert len(summary.errors) == 1
    assert summary.errors[0].startswith("wfcd cycles: WorldStateParseError")
    assert rig.cycles.next_change_at() is None
    assert "Strata Relay (Earth)" in await card_text(rig.outbox, arrival_key)


async def test_non_json_wfcd_falls_back_for_baro(db, clock, captured, sources, arrival_key):
    body = captured(WFCD)
    sources[SETTINGS.wfcd_url] = ok(body[: len(body) // 2])
    rig = make_rig(db, clock, sources)

    summary = await rig.poller.poll_once()

    assert summary.wfcd_ok is False
    assert [error.split(":")[0] for error in summary.errors] == ["wfcd cycles", "wfcd baro"]
    assert "Earth relay" in await card_text(rig.outbox, arrival_key)


async def test_item_database_without_names_keeps_cache(db, clock, captured, sources, real_names):
    items = json.loads(captured(ITEMS))
    for entry in items:
        entry.pop("name", None)
    sources[SETTINGS.items_url] = ok(json.dumps(items).encode())
    cached_at = clock.now() - timedelta(hours=25)
    rig = make_rig(db, clock, sources)
    await rig.names.replace_all(real_names, cached_at)

    summary = await rig.poller.poll_once()

    assert summary.items_refreshed is False
    assert len(summary.errors) == 1
    assert summary.errors[0].startswith("items: WorldStateParseError")
    assert await rig.names.all() == real_names
    assert await rig.names.fetched_at() == cached_at


async def test_baro_failure_does_not_stop_cycles_or_items(db, clock, captured, sources, caplog):
    class BrokenOutbox(OutboxRepo):
        """An outbox whose inserts fail, as a full disk would."""

        async def enqueue(self, kind, dedup_key, channel, payload):  # type: ignore[override]
            raise sqlite3.OperationalError("database or disk is full")

    rig = make_rig(db, clock, sources, outbox=BrokenOutbox(db, clock))

    with caplog.at_level(logging.ERROR, logger="amir.worldstate.poller"):
        summary = await rig.poller.poll_once()

    assert summary.official_ok and summary.wfcd_ok and summary.items_refreshed
    assert len(summary.errors) == 1
    assert summary.errors[0].startswith("baro: OperationalError")
    assert any("OperationalError" in r.getMessage() for r in caplog.records)
    assert await shown(rig.cards) == captured_cycles(captured)


async def test_cycle_failure_does_not_stop_baro(db, clock, sources, arrival_key, caplog):
    class BrokenCycles(CycleService):
        async def apply_snapshots(self, snaps):  # type: ignore[override]
            raise RuntimeError("cycle bug")

    rig = make_rig(db, clock, sources, cycles_class=BrokenCycles)

    with caplog.at_level(logging.ERROR, logger="amir.worldstate.poller"):
        summary = await rig.poller.poll_once()

    assert summary.official_ok and summary.wfcd_ok and summary.items_refreshed
    assert summary.errors == ["cycles: RuntimeError: cycle bug"]
    assert any("RuntimeError" in r.getMessage() for r in caplog.records)
    assert await outbox_keys(db) == [arrival_key]


async def test_sleep_targets_next_flip(db, clock, sources):
    rig = make_rig(db, clock, sources, settings=WorldStateSettings(interval_seconds=3600))
    await rig.poller.poll_once()

    assert clock.now() == at(25, 1, 21)
    assert rig.poller.next_sleep_seconds() == (12 * 60 + 28) + 5 == 753


async def test_sleep_capped_by_interval(db, clock, sources):
    rig = make_rig(db, clock, sources)
    await rig.poller.poll_once()

    assert rig.poller.next_sleep_seconds() == SETTINGS.interval_seconds == 300


async def test_sleep_is_interval_before_any_snapshot(db, clock, sources):
    rig = make_rig(db, clock, sources, settings=WorldStateSettings(interval_seconds=3600))

    assert rig.poller.next_sleep_seconds() == 3600


async def test_sleep_never_below_one_second(db, clock, sources):
    settings = WorldStateSettings(interval_seconds=3600, flip_refresh_delay_seconds=0)
    rig = make_rig(db, clock, sources, settings=settings)
    await rig.poller.poll_once()
    clock.advance((at(25, 1, 33, 28) - clock.now()).total_seconds() - 0.5)

    assert rig.poller.next_sleep_seconds() == 1


async def test_run_survives_a_crashed_poll_and_sleeps_until_the_flip(db, clock, sources, caplog):
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) == 2:
            raise asyncio.CancelledError

    class CrashOnce(WorldStatePoller):
        crashed = False

        async def poll_once(self) -> PollSummary:
            if not self.crashed:
                self.crashed = True
                raise RuntimeError("poll bug")
            return await super().poll_once()

    settings = WorldStateSettings(interval_seconds=3600)
    rig = make_rig(db, clock, sources, settings=settings, poller_class=CrashOnce, sleep=sleep)

    with caplog.at_level(logging.ERROR, logger="amir.worldstate.poller"):
        with pytest.raises(asyncio.CancelledError):
            await rig.poller.run()

    # The crashed poll waits a full interval; the next one wakes 5 s after the vallis flip.
    assert sleeps == [3600, 753]
    assert any("RuntimeError" in r.getMessage() for r in caplog.records)
    assert rig.fetcher.count(SETTINGS.wfcd_url) == 1


async def test_last_summary_and_latest_visit_are_kept(db, clock, captured, sources):
    rig = make_rig(db, clock, sources)
    assert rig.poller.last_summary is None
    assert rig.poller.last_visit is None

    first = await rig.poller.poll_once()

    assert rig.poller.last_summary is first
    assert rig.poller.last_visit == parse_void_trader(captured(OFFICIAL))

    rig.fetcher.responses[SETTINGS.official_url] = FetchError("http", 503)
    second = await rig.poller.poll_once()

    assert rig.poller.last_summary is second
    assert second.official_ok is False
    assert rig.poller.last_visit == parse_void_trader(captured(OFFICIAL))  # still the latest known
