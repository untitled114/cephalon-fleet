"""World-state polling: feeds the cycle cards and Baro Ki'Teer, and caches WFCD's item names.

Each poll fetches the official world state and WFCD's concurrently. The cycle service is fed as
soon as WFCD answers, whatever the official fetch is doing; Baro, who needs both, comes after.
Every fetch has a deadline (`OFFICIAL_DEADLINE`, `WFCD_DEADLINE`, `ITEMS_DEADLINE`), because
the HTTP timeouts only bound silence, not a whole slow download. Everything is isolated: a source
that fails, overruns its deadline or sends an unusable body, or a service that raises, is logged
and listed in the poll's errors, and the rest still runs. WFCD's snapshots are passed through as
they come. They can be over an hour old; the cycle service ignores stale ones and rolls forward
locally.

The item-name database (about 1.8 MB, and sometimes slow) is refreshed when it is
`items_refresh_hours` old, only after cycles and Baro are fed, so its slowness never delays them.
While the cache has never been filled it is fetched after cycles but before Baro, on every poll
until it succeeds: Baro's arrival card is queued once, and must not miss the real item names.
A failed refresh of a filled cache keeps the cached names and is retried no sooner than
`ITEMS_RETRY_AFTER` later. The name index is built from the cache once, and rebuilt only after a
refresh replaces it.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal, Protocol

from amir.baro.names import ItemNameIndex
from amir.baro.service import BaroService
from amir.clock import Clock
from amir.config.settings import WorldStateSettings
from amir.cycles.service import CycleService
from amir.net.http import FetchError, FetchResult
from amir.storage.repos import ItemNameRepo
from amir.worldstate.official import BaroVisit, WorldStateParseError, parse_void_trader
from amir.worldstate.wfcd import (
    CycleSnapshot,
    parse_baro_location,
    parse_cycles,
    parse_inventory_names,
    parse_item_names,
)

log = logging.getLogger(__name__)

# Seconds a whole fetch may take, retries included; past it the fetch is cancelled and recorded.
OFFICIAL_DEADLINE = 45.0
WFCD_DEADLINE = 45.0
ITEMS_DEADLINE = 120.0
ITEMS_RETRY_AFTER = timedelta(hours=1)
_ERROR_TEXT_LIMIT = 200

ItemsDue = Literal["first_fill", "refresh"]


class Fetcher(Protocol):
    async def fetch(
        self, url: str, *, etag: str | None = None, last_modified: str | None = None
    ) -> FetchResult: ...


@dataclass(frozen=True)
class PollSummary:
    """One poll's outcome. A source is ok when it was fetched and fully parsed."""

    official_ok: bool
    wfcd_ok: bool
    items_refreshed: bool
    errors: list[str]


@dataclass
class _Wfcd:
    ok: bool = False
    cycles: dict[str, CycleSnapshot] | None = None
    location: tuple[datetime, str] | None = None
    names: dict[str, str] = field(default_factory=dict)


class WorldStatePoller:
    def __init__(
        self,
        fetcher: Fetcher,
        settings: WorldStateSettings,
        baro: BaroService,
        cycles: CycleService,
        names: ItemNameRepo,
        clock: Clock,
        *,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._fetcher = fetcher
        self._settings = settings
        self._baro = baro
        self._cycles = cycles
        self._names = names
        self._clock = clock
        self._sleep = sleep
        self._index: ItemNameIndex | None = None
        # After a failed refresh of a filled cache: no new attempt before this moment.
        self._items_retry_at: datetime | None = None
        self._last_summary: PollSummary | None = None
        self._last_visit: BaroVisit | None = None

    @property
    def last_summary(self) -> PollSummary | None:
        """The latest poll's outcome; None before the first poll finishes."""
        return self._last_summary

    @property
    def last_visit(self) -> BaroVisit | None:
        """Baro's latest known visit, kept while later polls fail or list none."""
        return self._last_visit

    async def run(self) -> None:
        """Poll forever, sleeping `next_sleep_seconds()` between polls, until cancelled."""
        while True:
            delay = float(self._settings.interval_seconds)
            try:
                await self.poll_once()
                delay = self.next_sleep_seconds()
            except Exception as exc:
                log.error(
                    "world-state poll crashed error=%s; next poll in %ss",
                    type(exc).__name__,
                    delay,
                    exc_info=exc,
                )
            await self._sleep(delay)

    async def poll_once(self) -> PollSummary:
        official_errors: list[str] = []
        wfcd_errors: list[str] = []
        (official_ok, visit), parsed = await asyncio.gather(
            self._official(official_errors), self._wfcd_then_cycles(wfcd_errors)
        )
        if visit is not None:
            self._last_visit = visit
        errors = official_errors + wfcd_errors
        due = await self._items_due(errors)
        items_refreshed = False
        if due == "first_fill":
            items_refreshed = await self._refresh_items(errors)
        if visit is not None:
            await self._feed_baro(visit, parsed, errors)
        if due == "refresh":
            items_refreshed = await self._refresh_items(errors)
        summary = PollSummary(official_ok, parsed.ok, items_refreshed, errors)
        self._last_summary = summary
        log.debug(
            "world-state poll official_ok=%s wfcd_ok=%s items_refreshed=%s errors=%d",
            official_ok,
            parsed.ok,
            items_refreshed,
            len(errors),
        )
        return summary

    def next_sleep_seconds(self) -> float:
        """Until the next poll: the interval, or 5 s past the next cycle change if sooner."""
        interval = float(self._settings.interval_seconds)
        change_at = self._cycles.next_change_at()
        if change_at is None:
            return interval
        until = (change_at - self._clock.now()).total_seconds()
        return max(1.0, min(interval, until + self._settings.flip_refresh_delay_seconds))

    async def _official(self, errors: list[str]) -> tuple[bool, BaroVisit | None]:
        body = await self._get(self._settings.official_url, OFFICIAL_DEADLINE)
        return self._read_official(body, errors)

    async def _wfcd_then_cycles(self, errors: list[str]) -> _Wfcd:
        body = await self._get(self._settings.wfcd_url, WFCD_DEADLINE)
        parsed = self._read_wfcd(body, errors)
        await self._feed_cycles(parsed.cycles, errors)
        return parsed

    async def _fetch_within(self, url: str, deadline: float) -> FetchResult:
        try:
            return await asyncio.wait_for(self._fetcher.fetch(url), deadline)
        except TimeoutError:
            raise TimeoutError(f"no answer within {deadline:g} s") from None

    async def _get(self, url: str, deadline: float) -> bytes | Exception:
        """The body, or the exception: one source failing never cancels the other's fetch."""
        try:
            result = await self._fetch_within(url, deadline)
        except Exception as exc:
            return exc
        return result.body or b""

    def _read_official(
        self, body: bytes | Exception, errors: list[str]
    ) -> tuple[bool, BaroVisit | None]:
        if isinstance(body, Exception):
            self._failed(errors, "official", body)
            return False, None
        try:
            return True, parse_void_trader(body)
        except Exception as exc:  # hostile bodies raise more than WorldStateParseError
            self._failed(errors, "official", exc)
            return False, None

    def _read_wfcd(self, body: bytes | Exception, errors: list[str]) -> _Wfcd:
        """Cycles and Baro's part are read separately, so either survives the other's error."""
        parsed = _Wfcd()
        if isinstance(body, Exception):
            self._failed(errors, "wfcd", body)
            return parsed
        parsed.ok = True
        try:
            parsed.cycles = parse_cycles(body)
        except Exception as exc:
            parsed.ok = False
            self._failed(errors, "wfcd cycles", exc)
        try:
            parsed.location = parse_baro_location(body)
            parsed.names = parse_inventory_names(body)
        except Exception as exc:
            parsed.ok = False
            parsed.location = None
            self._failed(errors, "wfcd baro", exc)
        return parsed

    async def _feed_cycles(
        self, snapshots: dict[str, CycleSnapshot] | None, errors: list[str]
    ) -> None:
        try:
            if snapshots is not None:
                await self._cycles.apply_snapshots(snapshots)
            await self._cycles.tick()
        except Exception as exc:
            self._failed(errors, "cycles", exc)

    async def _feed_baro(self, visit: BaroVisit, wfcd: _Wfcd, errors: list[str]) -> None:
        try:
            if self._index is None:
                self._index = ItemNameIndex(await self._names.all())
            await self._baro.tick(visit, wfcd.location, wfcd.names, self._index)
        except Exception as exc:
            self._failed(errors, "baro", exc)

    async def _items_due(self, errors: list[str]) -> ItemsDue | None:
        """Whether the item names should be fetched this poll, and if so, why."""
        try:
            fetched_at = await self._names.fetched_at()
        except Exception as exc:
            self._failed(errors, "items", exc)
            return None
        if fetched_at is None:
            return "first_fill"  # no backoff: every poll tries until the cache is filled
        now = self._clock.now()
        if self._items_retry_at is not None and now < self._items_retry_at:
            return None
        if now - fetched_at < timedelta(hours=self._settings.items_refresh_hours):
            return None
        return "refresh"

    async def _refresh_items(self, errors: list[str]) -> bool:
        """Fetch and cache the item names; True on success."""
        try:
            result = await self._fetch_within(self._settings.items_url, ITEMS_DEADLINE)
            names = parse_item_names(result.body or b"")
            if not names:
                # Never trade a working cache for an empty one.
                raise WorldStateParseError("item database lists no names")
            index = ItemNameIndex(names)
            await self._names.replace_all(names, self._clock.now())
        except Exception as exc:
            self._failed(errors, "items", exc)
            self._items_retry_at = self._clock.now() + ITEMS_RETRY_AFTER
            return False
        self._index = index
        self._items_retry_at = None
        log.info("item names refreshed count=%d", len(names))
        return True

    @staticmethod
    def _failed(errors: list[str], what: str, exc: Exception) -> None:
        """Log and record one failure. A source failing or overrunning is expected; anything else
        may be a bug, so its trace is kept."""
        expected = isinstance(exc, FetchError | WorldStateParseError | TimeoutError)
        text = f"{type(exc).__name__}: {exc}"[:_ERROR_TEXT_LIMIT]
        log.log(
            logging.WARNING if expected else logging.ERROR,
            "world-state %s failed error=%s",
            what,
            text,
            exc_info=None if expected else exc,
        )
        errors.append(f"{what}: {text}")
