import re
from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from amir.news.canonical import canonicalize_url
from amir.news.forum_rss import FeedParseError, parse_forum_rss

FEED = "forum_pc_update_notes.rss.xml"


def test_real_forum_feed(captured: Callable[[str], bytes]) -> None:
    result = parse_forum_rss(captured(FEED), "forum")
    assert len(result.items) == 25
    newest = max(result.items, key=lambda i: i.published_at)
    assert newest.external_id == "1529796"
    assert newest.title == "Iceblade of Narin: The Icebind: Hotfix 44.1.3"
    assert newest.published_at == datetime(2026, 10, 22, 19, 4, 17, tzinfo=UTC)
    assert sum(1 for i in result.items if i.category == "Hotfix") == 20
    assert all(i.source_id == "forum" for i in result.items)
    assert all(i.canonical_url == canonicalize_url(i.url) for i in result.items)


def test_bad_date_tolerated(captured: Callable[[str], bytes]) -> None:
    data = captured(FEED)
    first = re.search(rb"<pubDate>.*?</pubDate>", data).group(0)
    broken = data.replace(first, b"<pubDate>not a date</pubDate>", 1)
    result = parse_forum_rss(broken, "forum")
    assert len(result.items) == 25
    assert result.problems
    assert sum(1 for i in result.items if i.published_at is None) == 1


def test_missing_guid_falls_back_to_link(captured: Callable[[str], bytes]) -> None:
    data = captured(FEED)
    original = parse_forum_rss(data, "forum").items[0]
    guid = re.search(rb"<guid[^>]*>.*?</guid>", data).group(0)
    result = parse_forum_rss(data.replace(guid, b"", 1), "forum")
    first = result.items[0]
    assert first.title == original.title
    assert first.external_id == canonicalize_url(first.url)
    assert first.external_id != original.external_id


def test_truncated_feed_does_not_crash(captured: Callable[[str], bytes]) -> None:
    try:
        result = parse_forum_rss(captured(FEED)[:200_000], "forum")
    except FeedParseError:
        return
    assert result.problems


def test_missing_title_skips_entry_with_problem(captured: Callable[[str], bytes]) -> None:
    data = captured(FEED)
    title = re.search(rb"<item>.*?(<title>.*?</title>)", data, re.DOTALL).group(1)
    result = parse_forum_rss(data.replace(title, b"", 1), "forum")
    assert len(result.items) == 24
    assert result.problems


def test_html_body_rejected() -> None:
    with pytest.raises(FeedParseError):
        parse_forum_rss(b"<html><body>Maintenance</body></html>", "forum")


def test_empty_body_rejected() -> None:
    with pytest.raises(FeedParseError):
        parse_forum_rss(b"", "forum")


def test_unusable_link_does_not_kill_feed(
    captured: Callable[[str], bytes], monkeypatch: pytest.MonkeyPatch
) -> None:
    import amir.news.forum_rss as mod

    real = mod.canonicalize_url
    calls = {"n": 0}

    def flaky(url: str) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("bad")
        return real(url)

    monkeypatch.setattr(mod, "canonicalize_url", flaky)
    result = parse_forum_rss(captured(FEED), "forum")
    assert len(result.items) == 24
    assert result.problems


def test_bad_link_in_feed_keeps_entry(captured: Callable[[str], bytes]) -> None:
    data = captured(FEED)
    link = re.search(rb"<item>.*?<link>(.*?)</link>", data, re.DOTALL).group(1)
    result = parse_forum_rss(data.replace(link, b"http://host:abc/x", 1), "forum")
    assert len(result.items) == 25
    assert result.items[0].canonical_url == "http://host:abc/x"
