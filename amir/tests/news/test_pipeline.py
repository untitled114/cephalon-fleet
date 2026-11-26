import asyncio
import hashlib
import json
import logging
import re
import tomllib
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

import amir.news.pipeline as pipeline_module
from amir.config.settings import NewsSettings, NewsSourceSettings
from amir.net.http import FetchError, FetchResult
from amir.news.cards import news_card
from amir.news.forum_rss import parse_forum_rss
from amir.news.pipeline import STALE_AFTER, NewsPipeline, PollReport
from amir.publishing.gate import ChannelKey
from amir.storage.db import Database
from amir.storage.repos import NewsItemRepo, OutboxRepo, SourceStateRepo

FORUM_CAPTURE = "forum_pc_update_notes.rss.xml"
STEAM_CAPTURE = "steam_getnewsforapp_announcements.json"
WORLDSTATE_CAPTURE = "official_worldstate_baro_present.json"

# Sources exactly as shipped in the example config.
EXAMPLE_CONFIG = Path(__file__).resolve().parents[2] / "config" / "amir.example.toml"
NEWS = NewsSettings.model_validate(tomllib.loads(EXAMPLE_CONFIG.read_text("utf-8"))["news"])
FORUM = next(s for s in NEWS.sources if s.id == "forum-pc-updates")
STEAM = next(s for s in NEWS.sources if s.id == "steam-announcements")

NEWEST_GUID = "1529796"  # Hotfix 44.1.3, 2026-10-22, the newest topic in the forum capture
# Update 44, 2026-09-23. The window is activity-ordered, so it sits above newer hotfixes.
OLD_GUID = "1523956"
KEY = f"news:{FORUM.id}:{NEWEST_GUID}"
# A day after the newest Steam item, when its two newest announcements are both under 7 days old.
STEAM_POLL_TIME = datetime(2026, 10, 20, tzinfo=UTC)


class FakeFetcher:
    """HttpFetcher stand-in: url -> FetchResult or exception; a list plays back in order."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = dict(responses)
        self.calls: list[tuple[str, str | None, str | None]] = []

    async def fetch(
        self, url: str, *, etag: str | None = None, last_modified: str | None = None
    ) -> FetchResult:
        self.calls.append((url, etag, last_modified))
        response = self.responses[url]
        if isinstance(response, list):
            response = response.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def ok(body: bytes, etag: str | None = None, last_modified: str | None = None) -> FetchResult:
    return FetchResult(200, body, etag, last_modified, False)


def forum_without(rss: bytes, *guids: str) -> bytes:
    """A copy of the real feed with the <item> elements for `guids` removed."""
    needles = [f">{guid}</guid>".encode() for guid in guids]
    removed = 0

    def drop(match: re.Match[bytes]) -> bytes:
        nonlocal removed
        if any(needle in match.group(0) for needle in needles):
            removed += 1
            return b""
        return match.group(0)

    out = re.sub(rb"<item>.*?</item>", drop, rss, flags=re.S)
    assert removed == len(guids)
    return out


def steam_derived(capture: bytes, mutate: Callable[[list[dict[str, Any]]], None]) -> bytes:
    """A copy of the real Steam capture with its newsitems list changed by `mutate`."""
    data = json.loads(capture)
    mutate(data["appnews"]["newsitems"])
    return json.dumps(data).encode()


def steam_without_first(capture: bytes, count: int) -> bytes:
    def drop(items: list[dict[str, Any]]) -> None:
        del items[:count]

    return steam_derived(capture, drop)


def etag_of(body: bytes) -> str:
    """A validator derived from the real bytes, as a server's content-hash ETag would be."""
    return '"' + hashlib.sha256(body).hexdigest()[:16] + '"'


def newest_pubdate(rss: bytes) -> str:
    return re.search(rb"<pubDate>(.*?)</pubDate>", rss).group(1).decode()


def make_pipeline(
    db: Database,
    clock: Any,
    fetcher: FakeFetcher,
    settings: NewsSettings,
    *,
    states: SourceStateRepo | None = None,
    outbox: OutboxRepo | None = None,
    **kwargs: Any,
) -> NewsPipeline:
    return NewsPipeline(
        fetcher,
        states or SourceStateRepo(db, clock),
        NewsItemRepo(db, clock),
        outbox or OutboxRepo(db, clock),
        settings,
        clock,
        **kwargs,
    )


def news_with(*sources: NewsSourceSettings, **changes: Any) -> NewsSettings:
    return NEWS.model_copy(update={"sources": list(sources), **changes})


async def outbox_keys(db: Database) -> list[str]:
    async with db.conn.execute("SELECT dedup_key FROM outbox ORDER BY id") as cur:
        return [row[0] for row in await cur.fetchall()]


async def news_rows(db: Database, source_id: str) -> dict[str, tuple[str, str]]:
    async with db.conn.execute(
        "SELECT external_id, status, title FROM news_items WHERE source_id = ?", (source_id,)
    ) as cur:
        return {row[0]: (row[1], row[2]) for row in await cur.fetchall()}


async def count_rows(db: Database, table: str) -> int:
    async with db.conn.execute(f"SELECT COUNT(*) FROM {table}") as cur:
        row = await cur.fetchone()
    return int(row[0])


async def test_first_poll_baselines_silently(db, clock, captured) -> None:
    fetcher = FakeFetcher({FORUM.url: ok(captured(FORUM_CAPTURE))})
    pipeline = make_pipeline(db, clock, fetcher, news_with(FORUM))

    report = await pipeline.poll_source(FORUM)

    assert report == PollReport(new=0, baseline=25, filtered=0, not_modified=False, error=None)
    assert pipeline.last_reports[FORUM.id] == report
    assert await outbox_keys(db) == []
    rows = await news_rows(db, FORUM.id)
    assert len(rows) == 25
    assert {status for status, _ in rows.values()} == {"baseline"}
    state = await SourceStateRepo(db, clock).get(FORUM.id)
    assert state.baseline_done is True
    assert state.last_attempt_at == clock.now()
    assert state.last_success_at == clock.now()
    assert state.last_error is None


async def test_new_item_enqueued_exactly_once(db, clock, captured) -> None:
    full = captured(FORUM_CAPTURE)
    fetcher = FakeFetcher({FORUM.url: ok(forum_without(full, NEWEST_GUID))})
    pipeline = make_pipeline(db, clock, fetcher, news_with(FORUM))

    first = await pipeline.poll_source(FORUM)
    assert (first.baseline, first.new) == (24, 0)

    item = next(i for i in parse_forum_rss(full, FORUM.id).items if i.external_id == NEWEST_GUID)
    assert clock.now() - item.published_at <= STALE_AFTER  # fresh, so the guard lets it post

    fetcher.responses[FORUM.url] = ok(full)
    second = await pipeline.poll_source(FORUM)
    assert (second.new, second.baseline, second.filtered) == (1, 0, 0)
    assert await outbox_keys(db) == [KEY]
    row = await OutboxRepo(db, clock).get_by_key(KEY)
    assert row is not None
    assert row.kind == "news"
    assert row.channel is ChannelKey.NEWS
    assert row.payload == {
        "cards": [news_card(item, source_label=FORUM.label).to_dict()],
        "text": None,
        "role_ping_id": None,
    }
    assert (await news_rows(db, FORUM.id))[NEWEST_GUID][0] == "queued"

    third = await pipeline.poll_source(FORUM)
    assert third.new == 0
    assert await outbox_keys(db) == [KEY]


async def test_restart_does_not_repost(tmp_path, clock, captured) -> None:
    path = tmp_path / "restart.sqlite3"
    full = captured(FORUM_CAPTURE)
    fetcher = FakeFetcher({FORUM.url: [ok(forum_without(full, NEWEST_GUID)), ok(full), ok(full)]})

    db = await Database.open(path)
    await db.migrate()
    try:
        pipeline = make_pipeline(db, clock, fetcher, news_with(FORUM))
        assert (await pipeline.poll_source(FORUM)).baseline == 24
        assert (await pipeline.poll_source(FORUM)).new == 1
    finally:
        await db.close()

    reopened = await Database.open(path)
    await reopened.migrate()
    try:
        restarted = make_pipeline(reopened, clock, fetcher, news_with(FORUM))
        report = await restarted.poll_source(FORUM)
        assert (report.new, report.baseline) == (0, 0)
        assert await outbox_keys(reopened) == [KEY]
    finally:
        await reopened.close()


async def test_not_modified_keeps_state(db, clock, captured) -> None:
    body = captured(FORUM_CAPTURE)
    # Validators derived from the real capture: a content hash and its newest pubDate.
    etag, last_modified = etag_of(body), newest_pubdate(body)
    fetcher = FakeFetcher({FORUM.url: ok(body, etag, last_modified)})
    pipeline = make_pipeline(db, clock, fetcher, news_with(FORUM))
    await pipeline.poll_source(FORUM)
    async with db.conn.execute("SELECT * FROM news_items ORDER BY id") as cur:
        before = [tuple(row) for row in await cur.fetchall()]

    clock.advance(300)
    fetcher.responses[FORUM.url] = FetchResult(304, None, None, None, True)
    report = await pipeline.poll_source(FORUM)

    assert report == PollReport(new=0, baseline=0, filtered=0, not_modified=True, error=None)
    assert fetcher.calls[-1] == (FORUM.url, etag, last_modified)
    state = await SourceStateRepo(db, clock).get(FORUM.id)
    assert state.last_success_at == clock.now()
    assert (state.etag, state.last_modified) == (etag, last_modified)
    async with db.conn.execute("SELECT * FROM news_items ORDER BY id") as cur:
        assert [tuple(row) for row in await cur.fetchall()] == before
    assert await outbox_keys(db) == []


@pytest.mark.parametrize(
    ("failure", "expected"),
    [(FetchError("timeout"), "timeout"), (FetchError("http", 503), "http 503")],
)
async def test_failure_isolated_between_sources(db, clock, captured, failure, expected) -> None:
    disabled = STEAM.model_copy(update={"id": "steam-disabled", "enabled": False})
    fetcher = FakeFetcher({FORUM.url: failure, STEAM.url: ok(captured(STEAM_CAPTURE))})
    pipeline = make_pipeline(db, clock, fetcher, news_with(FORUM, STEAM, disabled))

    reports = await pipeline.poll_all()

    assert set(reports) == {FORUM.id, STEAM.id}
    assert reports[FORUM.id].error == expected
    assert reports[STEAM.id] == PollReport(
        new=0, baseline=20, filtered=0, not_modified=False, error=None
    )
    assert pipeline.last_reports == reports
    states = SourceStateRepo(db, clock)
    forum_state = await states.get(FORUM.id)
    assert forum_state.last_error == expected
    assert forum_state.baseline_done is False
    assert (await states.get(STEAM.id)).baseline_done is True


class HangingFetcher(FakeFetcher):
    """A fetch that never answers (a stalled download) until it is cancelled."""

    def __init__(self, responses: dict[str, Any]) -> None:
        super().__init__(responses)
        self.cancelled = 0

    async def fetch(
        self, url: str, *, etag: str | None = None, last_modified: str | None = None
    ) -> FetchResult:
        if url in self.responses:
            return await super().fetch(url, etag=etag, last_modified=last_modified)
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise
        raise AssertionError("unreachable")


async def test_hanging_fetch_is_cut_off_at_the_deadline(
    db, clock, captured, caplog, monkeypatch
) -> None:
    monkeypatch.setattr(pipeline_module, "NEWS_DEADLINE", 0.05)
    fetcher = HangingFetcher({STEAM.url: ok(captured(STEAM_CAPTURE))})
    pipeline = make_pipeline(db, clock, fetcher, news_with(FORUM, STEAM))

    with caplog.at_level(logging.WARNING, logger="amir.news.pipeline"):
        reports = await asyncio.wait_for(pipeline.poll_all(), 5)

    assert reports[FORUM.id] == PollReport(error="timeout: no answer within 0.05 s")
    assert reports[STEAM.id].baseline == 20  # the other source is not held up
    assert fetcher.cancelled == 1
    state = await SourceStateRepo(db, clock).get(FORUM.id)
    assert state.last_error == reports[FORUM.id].error
    assert state.baseline_done is False
    assert await news_rows(db, FORUM.id) == {}
    assert any(FORUM.id in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)


def test_news_deadline_default() -> None:
    assert pipeline_module.NEWS_DEADLINE == 60


@pytest.mark.parametrize("which", ["forum", "steam"])
async def test_non_feed_body_leaves_state_untouched(db, clock, captured, caplog, which) -> None:
    forum_rss = captured(FORUM_CAPTURE)
    if which == "forum":
        # Real HTML: the body of the newest topic, lifted out of the capture's CDATA.
        src, good = FORUM, ok(forum_rss)
        bad = re.search(rb"<description><!\[CDATA\[(.*?)\]\]>", forum_rss, re.S).group(1)
    else:
        # Real JSON that is not a Steam news response.
        src, good = STEAM, ok(captured(STEAM_CAPTURE))
        bad = captured(WORLDSTATE_CAPTURE)
    # The bad response carries validators too (derived from the bytes); none may be stored.
    fetcher = FakeFetcher({src.url: ok(bad, etag_of(bad), newest_pubdate(forum_rss))})
    pipeline = make_pipeline(db, clock, fetcher, news_with(src))

    with caplog.at_level(logging.WARNING, logger="amir.news.pipeline"):
        report = await pipeline.poll_source(src)

    assert report.error
    assert (report.new, report.baseline, report.filtered) == (0, 0, 0)
    states = SourceStateRepo(db, clock)
    state = await states.get(src.id)
    assert state.baseline_done is False
    assert state.last_error == report.error
    assert (state.etag, state.last_modified) == (None, None)
    assert await count_rows(db, "news_items") == 0
    assert await outbox_keys(db) == []
    assert any(src.id in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)

    fetcher.responses[src.url] = good
    recovered = await pipeline.poll_source(src)
    assert recovered.baseline > 0 and recovered.error is None
    assert (await states.get(src.id)).last_error is None


async def test_excluded_items_recorded_not_queued(db, make_clock, captured, caplog) -> None:
    clock = make_clock(STEAM_POLL_TIME)
    full = captured(STEAM_CAPTURE)
    fetcher = FakeFetcher({STEAM.url: ok(steam_without_first(full, 2))})
    pipeline = make_pipeline(db, clock, fetcher, news_with(STEAM))
    assert (await pipeline.poll_source(STEAM)).baseline == 18

    fetcher.responses[STEAM.url] = ok(full)
    with caplog.at_level(logging.INFO, logger="amir.news.pipeline"):
        report = await pipeline.poll_source(STEAM)

    assert (report.new, report.filtered) == (0, 2)
    by_title = {title: status for status, title in (await news_rows(db, STEAM.id)).values()}
    assert by_title['"Devourer" Single Release'] == "filtered"
    assert by_title["Baro Ki'Teer Returns"] == "filtered"
    assert await outbox_keys(db) == []
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any(STEAM.id in m and "single release" in m and "Devourer" in m for m in messages), (
        messages
    )
    assert any(STEAM.id in m and "baro ki'teer" in m for m in messages), messages


async def test_forum_topics_are_not_title_filtered(db, clock, captured) -> None:
    # The keyword list is the Steam announcement filter; every forum topic is in scope.
    full = captured(FORUM_CAPTURE)
    fetcher = FakeFetcher({FORUM.url: ok(forum_without(full, NEWEST_GUID))})
    settings = news_with(FORUM, exclude_title_keywords=["hotfix"])
    pipeline = make_pipeline(db, clock, fetcher, settings)
    await pipeline.poll_source(FORUM)

    fetcher.responses[FORUM.url] = ok(full)
    report = await pipeline.poll_source(FORUM)

    assert (report.new, report.filtered) == (1, 0)
    assert await outbox_keys(db) == [KEY]


async def test_same_url_across_sources_posted_once(db, clock, captured) -> None:
    full = captured(FORUM_CAPTURE)
    twin = FORUM.model_copy(update={"id": "forum-pc-updates-twin"})
    fetcher = FakeFetcher({FORUM.url: ok(forum_without(full, NEWEST_GUID))})
    pipeline = make_pipeline(db, clock, fetcher, news_with(FORUM, twin))
    assert (await pipeline.poll_source(FORUM)).baseline == 24
    # Every URL is already recorded under the first source, so the twin stores no rows.
    assert (await pipeline.poll_source(twin)).baseline == 0
    assert (await SourceStateRepo(db, clock).get(twin.id)).baseline_done is True

    fetcher.responses[FORUM.url] = ok(full)
    assert (await pipeline.poll_source(FORUM)).new == 1
    assert (await pipeline.poll_source(twin)).new == 0

    assert await outbox_keys(db) == [KEY]
    assert NEWEST_GUID not in await news_rows(db, twin.id)


async def test_empty_url_is_never_treated_as_seen(db, make_clock, captured) -> None:
    clock = make_clock(STEAM_POLL_TIME)
    full = captured(STEAM_CAPTURE)

    def blank_urls(*indexes: int) -> Callable[[list[dict[str, Any]]], None]:
        def mutate(items: list[dict[str, Any]]) -> None:
            for index in indexes:
                items[index]["url"] = ""

        return mutate

    # Poll 1 stores an item whose URL is empty; poll 2 brings two more without URLs.
    first = steam_without_first(steam_derived(full, blank_urls(2)), 2)
    fetcher = FakeFetcher({STEAM.url: ok(first)})
    settings = news_with(STEAM, exclude_title_keywords=[])
    pipeline = make_pipeline(db, clock, fetcher, settings)
    assert (await pipeline.poll_source(STEAM)).baseline == 18

    fetcher.responses[STEAM.url] = ok(steam_derived(full, blank_urls(0, 1, 2)))
    report = await pipeline.poll_source(STEAM)

    assert report.new == 2
    assert len(await outbox_keys(db)) == 2


async def test_edited_title_not_reposted(db, clock, captured) -> None:
    full = captured(FORUM_CAPTURE)
    fetcher = FakeFetcher({FORUM.url: ok(forum_without(full, NEWEST_GUID))})
    pipeline = make_pipeline(db, clock, fetcher, news_with(FORUM))
    await pipeline.poll_source(FORUM)
    fetcher.responses[FORUM.url] = ok(full)
    assert (await pipeline.poll_source(FORUM)).new == 1

    # Same guid, new title: the newest topic retitled with the next topic's real title.
    titles = re.findall(rb"<item><title>(.*?)</title>", full)
    edited = full.replace(
        b"<title>" + titles[0] + b"</title>", b"<title>" + titles[1] + b"</title>", 1
    )
    fetcher.responses[FORUM.url] = ok(edited)
    report = await pipeline.poll_source(FORUM)

    assert report.new == 0
    assert await outbox_keys(db) == [KEY]
    status, title = (await news_rows(db, FORUM.id))[NEWEST_GUID]
    assert status == "queued"
    assert title == titles[0].decode()


async def test_new_items_enqueued_oldest_first(db, make_clock, captured) -> None:
    # Steam's two newest are 3 days apart (the forum's two newest are over 7 days apart).
    clock = make_clock(STEAM_POLL_TIME)
    full = captured(STEAM_CAPTURE)
    fetcher = FakeFetcher({STEAM.url: ok(steam_without_first(full, 2))})
    pipeline = make_pipeline(db, clock, fetcher, news_with(STEAM, exclude_title_keywords=[]))
    assert (await pipeline.poll_source(STEAM)).baseline == 18

    fetcher.responses[STEAM.url] = ok(full)
    assert (await pipeline.poll_source(STEAM)).new == 2

    newest, second = (item["gid"] for item in json.loads(full)["appnews"]["newsitems"][:2])
    assert await outbox_keys(db) == [f"news:{STEAM.id}:{second}", f"news:{STEAM.id}:{newest}"]


@pytest.mark.parametrize(
    ("categories", "role_id", "expected"),
    [(["Hotfix"], 7, 7), (["Hotfix"], 0, None), (["Update", "Announcement"], 7, None)],
)
async def test_role_ping_only_when_configured(
    db, clock, captured, categories, role_id, expected
) -> None:
    full = captured(FORUM_CAPTURE)
    fetcher = FakeFetcher({FORUM.url: ok(forum_without(full, NEWEST_GUID))})
    settings = news_with(FORUM, role_ping_categories=categories)
    pipeline = make_pipeline(db, clock, fetcher, settings, notification_role_id=role_id)
    await pipeline.poll_source(FORUM)
    fetcher.responses[FORUM.url] = ok(full)
    await pipeline.poll_source(FORUM)

    row = await OutboxRepo(db, clock).get_by_key(KEY)
    assert row is not None
    assert row.payload["role_ping_id"] == expected


async def test_empty_first_poll_does_not_baseline(db, clock, captured) -> None:
    full = captured(STEAM_CAPTURE)
    fetcher = FakeFetcher({STEAM.url: ok(steam_without_first(full, 20))})
    pipeline = make_pipeline(db, clock, fetcher, news_with(STEAM))

    empty = await pipeline.poll_source(STEAM)
    assert empty == PollReport(new=0, baseline=0, filtered=0, not_modified=False, error=None)
    states = SourceStateRepo(db, clock)
    assert (await states.get(STEAM.id)).baseline_done is False

    fetcher.responses[STEAM.url] = ok(full)
    report = await pipeline.poll_source(STEAM)
    assert (report.baseline, report.new) == (20, 0)
    assert await outbox_keys(db) == []
    assert (await states.get(STEAM.id)).baseline_done is True


async def test_parse_problems_logged(db, clock, captured, caplog) -> None:
    def blank_first_title(items: list[dict[str, Any]]) -> None:
        items[0]["title"] = ""

    body = steam_derived(captured(STEAM_CAPTURE), blank_first_title)
    fetcher = FakeFetcher({STEAM.url: ok(body)})
    pipeline = make_pipeline(db, clock, fetcher, news_with(STEAM))

    with caplog.at_level(logging.WARNING, logger="amir.news.pipeline"):
        report = await pipeline.poll_source(STEAM)

    assert (report.baseline, report.error) == (19, None)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(STEAM.id in m and "missing title" in m for m in warnings), warnings
    assert (await SourceStateRepo(db, clock).get(STEAM.id)).last_error is None


async def test_run_loops_each_enabled_source_and_survives_errors(db, clock, captured, caplog):
    disabled = FORUM.model_copy(update={"id": "forum-disabled", "enabled": False})
    fetcher = FakeFetcher(
        {
            FORUM.url: [RuntimeError("boom"), ok(captured(FORUM_CAPTURE))],
            STEAM.url: ok(captured(STEAM_CAPTURE)),
        }
    )
    slept: list[float] = []
    done = asyncio.Event()

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        if sorted(slept) == [FORUM.interval_seconds] * 2 + [STEAM.interval_seconds]:
            done.set()
        if seconds == FORUM.interval_seconds and slept.count(seconds) == 1:
            return  # let the forum loop come round once more after its crash
        await asyncio.Event().wait()  # park this loop

    settings = news_with(FORUM, STEAM, disabled)
    pipeline = make_pipeline(db, clock, fetcher, settings, sleep=sleep)
    with caplog.at_level(logging.ERROR, logger="amir.news.pipeline"):
        runner = asyncio.create_task(pipeline.run())
        await asyncio.wait_for(done.wait(), timeout=5)
        runner.cancel()
        with pytest.raises(asyncio.CancelledError):
            await runner

    assert pipeline.last_reports[FORUM.id].baseline == 25
    assert pipeline.last_reports[STEAM.id].baseline == 20
    assert disabled.id not in pipeline.last_reports
    assert [url for url, _, _ in fetcher.calls].count(FORUM.url) == 2
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any(FORUM.id in r.getMessage() and r.exc_info for r in errors)


async def test_old_item_entering_window_is_baselined_not_posted(db, clock, captured, caplog):
    # A reply bumps an old topic back into the activity-ordered window: never post it as new.
    full = captured(FORUM_CAPTURE)
    fetcher = FakeFetcher({FORUM.url: ok(forum_without(full, NEWEST_GUID, OLD_GUID))})
    pipeline = make_pipeline(db, clock, fetcher, news_with(FORUM))
    assert (await pipeline.poll_source(FORUM)).baseline == 23

    fetcher.responses[FORUM.url] = ok(full)
    with caplog.at_level(logging.INFO, logger="amir.news.pipeline"):
        report = await pipeline.poll_source(FORUM)

    assert (report.new, report.baseline, report.filtered) == (1, 1, 0)
    assert await outbox_keys(db) == [KEY]
    assert (await news_rows(db, FORUM.id))[OLD_GUID] == ("baseline", "Update 44: Iceblade of Narin")
    messages = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert any(
        FORUM.id in m
        and OLD_GUID in m
        and "Update 44: Iceblade of Narin" in m
        and "age_days=31.4" in m
        for m in messages
    ), messages


async def test_stale_announcement_is_baselined_not_filtered(db, clock, captured) -> None:
    # At the conftest clock (2026-10-25 01:21Z) item 0 is 5.4 days old and item 1 is 8.5.
    full = captured(STEAM_CAPTURE)
    fetcher = FakeFetcher({STEAM.url: ok(steam_without_first(full, 2))})
    pipeline = make_pipeline(db, clock, fetcher, news_with(STEAM))
    await pipeline.poll_source(STEAM)

    fetcher.responses[STEAM.url] = ok(full)
    report = await pipeline.poll_source(STEAM)

    assert (report.new, report.baseline, report.filtered) == (0, 1, 1)
    by_title = {title: status for status, title in (await news_rows(db, STEAM.id)).values()}
    assert by_title['"Devourer" Single Release'] == "filtered"
    assert by_title["Baro Ki'Teer Returns"] == "baseline"
    assert await outbox_keys(db) == []


async def test_undated_item_still_posts(db, make_clock, captured) -> None:
    clock = make_clock(datetime(2027, 1, 1, tzinfo=UTC))  # every dated item is long stale
    full = captured(FORUM_CAPTURE)
    first_pubdate = re.search(rb"<pubDate>.*?</pubDate>", full).group(0)
    undated = full.replace(first_pubdate, b"", 1)
    item = next(i for i in parse_forum_rss(undated, FORUM.id).items if i.external_id == NEWEST_GUID)
    assert item.published_at is None
    fetcher = FakeFetcher({FORUM.url: ok(forum_without(undated, NEWEST_GUID))})
    pipeline = make_pipeline(db, clock, fetcher, news_with(FORUM))
    assert (await pipeline.poll_source(FORUM)).baseline == 24

    fetcher.responses[FORUM.url] = ok(undated)
    report = await pipeline.poll_source(FORUM)

    assert (report.new, report.baseline) == (1, 0)
    assert await outbox_keys(db) == [KEY]


class FailingOutbox(OutboxRepo):
    """Raises on the `fail_on`-th enqueue, after the earlier ones were written."""

    def __init__(self, db: Database, clock: Any, fail_on: int) -> None:
        super().__init__(db, clock)
        self.calls = 0
        self._fail_on = fail_on

    async def enqueue(self, *args: Any, **kwargs: Any) -> bool:
        self.calls += 1
        if self.calls == self._fail_on:
            raise RuntimeError("enqueue failed")
        return await super().enqueue(*args, **kwargs)


async def test_poll_writes_are_atomic(db, make_clock, captured) -> None:
    clock = make_clock(STEAM_POLL_TIME)
    full = captured(STEAM_CAPTURE)
    partial = steam_without_first(full, 2)
    fetcher = FakeFetcher({STEAM.url: ok(partial, etag_of(partial))})
    outbox = FailingOutbox(db, clock, fail_on=2)
    settings = news_with(STEAM, exclude_title_keywords=[])
    pipeline = make_pipeline(db, clock, fetcher, settings, outbox=outbox)
    assert (await pipeline.poll_source(STEAM)).baseline == 18
    states = SourceStateRepo(db, clock)
    before = await states.get(STEAM.id)

    clock.advance(900)
    fetcher.responses[STEAM.url] = ok(full, etag_of(full))
    with pytest.raises(RuntimeError, match="enqueue failed"):
        await pipeline.poll_source(STEAM)

    assert outbox.calls == 2  # the first new item's rows were written, then rolled back
    assert await count_rows(db, "news_items") == 18
    assert await outbox_keys(db) == []
    after = await states.get(STEAM.id)
    assert (after.etag, after.last_success_at) == (etag_of(partial), before.last_success_at)
    assert db.conn.in_transaction is False
    async with asyncio.timeout(1):  # and the transaction lock was released
        async with db.transaction():
            pass

    report = await pipeline.poll_source(STEAM)

    assert fetcher.calls[-1][1] == etag_of(partial)  # unchanged validator: full body again
    assert report.new == 2
    gids = [item["gid"] for item in json.loads(full)["appnews"]["newsitems"][:2]]
    assert sorted(await outbox_keys(db)) == sorted(f"news:{STEAM.id}:{gid}" for gid in gids)
    assert (await states.get(STEAM.id)).etag == etag_of(full)


class FailingErrorWrites(SourceStateRepo):
    async def record_error(self, source_id: str, msg: str) -> None:
        raise RuntimeError("state write failed")


@pytest.mark.parametrize("state_write_fails", [False, True])
async def test_unexpected_error_recorded_best_effort(
    db, clock, captured, caplog, state_write_fails
) -> None:
    states = FailingErrorWrites(db, clock) if state_write_fails else SourceStateRepo(db, clock)
    fetcher = FakeFetcher({FORUM.url: RuntimeError("boom"), STEAM.url: ok(captured(STEAM_CAPTURE))})
    pipeline = make_pipeline(db, clock, fetcher, news_with(FORUM, STEAM), states=states)

    with caplog.at_level(logging.ERROR, logger="amir.news.pipeline"):
        reports = await pipeline.poll_all()

    assert reports[FORUM.id].error == "unexpected error: RuntimeError"
    assert reports[STEAM.id].baseline == 20
    stored = (await SourceStateRepo(db, clock).get(FORUM.id)).last_error
    failures = [r.exc_info[1] for r in caplog.records if r.exc_info and FORUM.id in r.getMessage()]
    if state_write_fails:
        assert stored is None
        assert [str(exc) for exc in failures] == ["boom", "state write failed"]
    else:
        assert stored == "unexpected error: RuntimeError"
        assert [str(exc) for exc in failures] == ["boom"]
