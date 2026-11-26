"""News polling: fetch each source, dedup durably, and queue new items for the news channel.

Per poll of one source, the fetch and the parse (in a worker thread) run with no transaction
open. Then every write for that source (news_items rows, source_state, outbox rows) happens
in one transaction, so a crash keeps all of a poll's effects or none of them. Sources poll
independently: one failing never stops another.

After a source's baseline, a never-seen item dated more than STALE_AFTER before the poll is
recorded as baseline, not posted. The forum window is ordered by activity, so a reply can bring
an old topic back into it; a larger window or a partial first parse would do the same.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from amir.clock import Clock
from amir.config.settings import NewsSettings, NewsSourceSettings
from amir.net.http import FetchError, FetchResult
from amir.news.cards import news_card
from amir.news.filtering import exclusion_reason
from amir.news.forum_rss import parse_forum_rss
from amir.news.model import FeedParseError, NewsItem, ParseResult
from amir.news.steam import parse_steam_news
from amir.publishing.gate import ChannelKey
from amir.storage.repos import NewsItemRepo, OutboxRepo, SourceStateRepo

log = logging.getLogger(__name__)

OUTBOX_KIND = "news"
# The title keyword list is the Steam announcement filter; every forum topic is in scope.
TITLE_FILTERED_KINDS = frozenset({"steam_news"})
# A never-seen item older than this at poll time is recorded silently, never posted.
STALE_AFTER = timedelta(days=7)
# A whole fetch, retries included, must answer within this many seconds: the HTTP timeouts
# only bound silence, not a slow trickle.
NEWS_DEADLINE = 60.0

_ERROR_TEXT_LIMIT = 200
_PARSERS: dict[str, Callable[[bytes, str], ParseResult]] = {
    "forum_rss": parse_forum_rss,
    "steam_news": parse_steam_news,
}
_UNDATED = datetime.max.replace(tzinfo=UTC)


class Fetcher(Protocol):
    async def fetch(
        self, url: str, *, etag: str | None = None, last_modified: str | None = None
    ) -> FetchResult: ...


@dataclass(frozen=True)
class PollReport:
    new: int = 0
    baseline: int = 0
    filtered: int = 0
    not_modified: bool = False
    error: str | None = None


def _oldest_first(item: NewsItem) -> datetime:
    return item.published_at or _UNDATED


class NewsPipeline:
    def __init__(
        self,
        fetcher: Fetcher,
        states: SourceStateRepo,
        items: NewsItemRepo,
        outbox: OutboxRepo,
        settings: NewsSettings,
        clock: Clock,
        *,
        notification_role_id: int = 0,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._fetcher = fetcher
        self._states = states
        self._items = items
        self._outbox = outbox
        self._settings = settings
        self._clock = clock
        self._role_id = notification_role_id
        self._sleep = sleep
        self.last_reports: dict[str, PollReport] = {}

    async def run(self) -> None:
        """Poll every enabled source forever, each on its own interval, until cancelled."""
        async with asyncio.TaskGroup() as group:
            for src in self._enabled():
                group.create_task(self._loop(src), name=f"news:{src.id}")

    async def poll_all(self) -> dict[str, PollReport]:
        """Poll every enabled source once, concurrently; a failure stays with its source."""
        sources = self._enabled()
        reports = await asyncio.gather(*(self._poll_isolated(src) for src in sources))
        return {src.id: report for src, report in zip(sources, reports, strict=True)}

    async def poll_source(self, src: NewsSourceSettings) -> PollReport:
        report = await self._poll(src)
        self.last_reports[src.id] = report
        return report

    def _enabled(self) -> list[NewsSourceSettings]:
        return [src for src in self._settings.sources if src.enabled]

    async def _loop(self, src: NewsSourceSettings) -> None:
        while True:
            await self._poll_isolated(src)
            await self._sleep(src.interval_seconds)

    async def _poll_isolated(self, src: NewsSourceSettings) -> PollReport:
        try:
            return await self.poll_source(src)
        except Exception as exc:
            log.exception("news poll crashed source=%s", src.id)
            error = f"unexpected error: {type(exc).__name__}"
            try:
                await self._states.record_error(src.id, error)
            except Exception:
                log.exception("news could not record error state source=%s", src.id)
            report = PollReport(error=error)
            self.last_reports[src.id] = report
            return report

    async def _poll(self, src: NewsSourceSettings) -> PollReport:
        state = await self._states.get(src.id)
        await self._states.record_attempt(src.id)
        try:
            result = await asyncio.wait_for(
                self._fetcher.fetch(src.url, etag=state.etag, last_modified=state.last_modified),
                NEWS_DEADLINE,
            )
        except (FetchError, TimeoutError) as exc:
            if isinstance(exc, FetchError):
                error = exc.kind if exc.status is None else f"{exc.kind} {exc.status}"
            else:
                error = f"timeout: no answer within {NEWS_DEADLINE:g} s"
            log.warning("news fetch failed source=%s error=%s", src.id, error)
            await self._states.record_error(src.id, error)
            return PollReport(error=error)

        if result.not_modified:
            await self._states.record_success(
                src.id, result.etag or state.etag, result.last_modified or state.last_modified
            )
            return PollReport(not_modified=True)

        try:
            # CPU-bound (about half a second on the real forum feed): keep it off the loop.
            parsed = await asyncio.to_thread(_PARSERS[src.kind], result.body or b"", src.id)
        except FeedParseError as exc:
            error = f"not a usable feed: {exc}"[:_ERROR_TEXT_LIMIT]
            log.warning("news parse failed source=%s error=%s", src.id, error)
            await self._states.record_error(src.id, error)
            return PollReport(error=error)
        for problem in parsed.problems:
            log.warning("news parse problem source=%s problem=%s", src.id, problem)
        return await self._store(src, parsed, result)

    async def _store(
        self, src: NewsSourceSettings, parsed: ParseResult, result: FetchResult
    ) -> PollReport:
        new = baseline = filtered = 0
        now = self._clock.now()
        # One transaction for the whole poll. Every write below is awaited in this task,
        # so the repos join it; a spawned task would wait on the lock this one holds.
        async with self._items.db.transaction():
            first = not (await self._states.get(src.id)).baseline_done
            for item in sorted(parsed.items, key=_oldest_first):
                if await self._seen(src, item):
                    continue
                if first:
                    await self._items.add(item, "baseline")
                    baseline += 1
                    continue
                if item.published_at is not None and now - item.published_at > STALE_AFTER:
                    log.info(
                        "news item too old to post source=%s id=%s title=%r age_days=%.1f",
                        src.id,
                        item.external_id,
                        item.title,
                        (now - item.published_at) / timedelta(days=1),
                    )
                    await self._items.add(item, "baseline")
                    baseline += 1
                    continue
                keyword = self._exclusion(src, item)
                if keyword is not None:
                    log.info(
                        "news item filtered source=%s keyword=%r title=%r",
                        src.id,
                        keyword,
                        item.title,
                    )
                    await self._items.add(item, "filtered")
                    filtered += 1
                    continue
                await self._items.add(item, "queued")
                if await self._outbox.enqueue(
                    OUTBOX_KIND,
                    f"news:{src.id}:{item.external_id}",
                    ChannelKey.NEWS,
                    self._payload(src, item),
                ):
                    new += 1
                    log.info("news item queued source=%s id=%s", src.id, item.external_id)
            if first and parsed.items:
                await self._states.mark_baseline_done(src.id)
            await self._states.record_success(src.id, result.etag, result.last_modified)
        if first:
            if parsed.items:
                log.info("news baseline done source=%s items=%d", src.id, baseline)
            else:
                log.warning("news source=%s returned no items; baseline deferred", src.id)
        return PollReport(new=new, baseline=baseline, filtered=filtered)

    async def _seen(self, src: NewsSourceSettings, item: NewsItem) -> bool:
        if await self._items.exists(src.id, item.external_id):
            return True
        return await self._items.url_seen(item.canonical_url)

    def _exclusion(self, src: NewsSourceSettings, item: NewsItem) -> str | None:
        if src.kind not in TITLE_FILTERED_KINDS:
            return None
        return exclusion_reason(item.title, self._settings.exclude_title_keywords)

    def _payload(self, src: NewsSourceSettings, item: NewsItem) -> dict[str, Any]:
        ping = self._role_id != 0 and item.category in self._settings.role_ping_categories
        return {
            "cards": [news_card(item, source_label=src.label).to_dict()],
            "text": None,
            "role_ping_id": self._role_id if ping else None,
        }
