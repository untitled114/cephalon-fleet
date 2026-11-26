from datetime import UTC, datetime

import feedparser

from amir.news.canonical import canonicalize_url
from amir.news.model import FeedParseError, NewsItem, ParseResult
from amir.publishing.sanitize import structured_excerpt

__all__ = ["FeedParseError", "parse_forum_rss"]


def _published(entry: feedparser.FeedParserDict) -> datetime | None:
    parsed = entry.get("published_parsed")
    if not parsed:
        return None
    return datetime(*parsed[:6], tzinfo=UTC)


def parse_forum_rss(data: bytes, source_id: str) -> ParseResult:
    feed = feedparser.parse(data)
    if not feed.entries:
        raise FeedParseError(f"no feed entries found (bozo={bool(feed.get('bozo'))})")
    result = ParseResult()
    if feed.get("bozo"):
        result.problems.append(f"feed is malformed: {feed.get('bozo_exception')!r}")
    for index, entry in enumerate(feed.entries):
        title = (entry.get("title") or "").strip()
        link = (entry.get("link") or "").strip()
        if not title:
            result.problems.append(f"entry {index}: missing title, skipped")
            continue
        if not link:
            result.problems.append(f"entry {index}: missing link, skipped")
            continue
        try:
            canonical = canonicalize_url(link)
        except ValueError:
            result.problems.append(f"entry {index}: unusable link, skipped")
            continue
        published_at = _published(entry)
        if published_at is None:
            result.problems.append(f"entry {index}: missing or unparseable date")
        result.items.append(
            NewsItem(
                source_id=source_id,
                external_id=str(entry.get("id") or canonical),
                title=title,
                url=link,
                canonical_url=canonical,
                published_at=published_at,
                excerpt=structured_excerpt(entry.get("summary") or ""),
                category="Hotfix" if "hotfix" in title.lower() else "Update",
                author=entry.get("author") or None,
            )
        )
    return result
