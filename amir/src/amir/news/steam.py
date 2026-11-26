import json
from datetime import UTC, datetime

from amir.news.canonical import canonicalize_url
from amir.news.model import FeedParseError, NewsItem, ParseResult
from amir.publishing.sanitize import excerpt, plain_text

__all__ = ["FeedParseError", "parse_steam_news"]

_ANNOUNCEMENTS_FEED = "steam_community_announcements"


def _published(value: object) -> datetime | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    try:
        return datetime.fromtimestamp(value, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def parse_steam_news(data: bytes, source_id: str) -> ParseResult:
    try:
        payload = json.loads(data)
    except ValueError as exc:
        raise FeedParseError(f"body is not JSON: {exc}") from exc
    appnews = payload.get("appnews") if isinstance(payload, dict) else None
    newsitems = appnews.get("newsitems") if isinstance(appnews, dict) else None
    if not isinstance(newsitems, list):
        raise FeedParseError("missing appnews.newsitems")
    result = ParseResult()
    for index, item in enumerate(newsitems):
        if not isinstance(item, dict) or item.get("feedname") != _ANNOUNCEMENTS_FEED:
            continue
        gid = str(item.get("gid") or "").strip()
        title = str(item.get("title") or "").strip()
        if not gid:
            result.problems.append(f"item {index}: missing gid, skipped")
            continue
        if not title:
            result.problems.append(f"item {index}: missing title, skipped")
            continue
        url = str(item.get("url") or "").strip()
        published_at = _published(item.get("date"))
        if published_at is None:
            result.problems.append(f"item {index}: missing or invalid date")
        result.items.append(
            NewsItem(
                source_id=source_id,
                external_id=gid,
                title=title,
                url=url,
                canonical_url=canonicalize_url(url),
                published_at=published_at,
                excerpt=excerpt(plain_text(str(item.get("contents") or ""))),
                category="Announcement",
                author=item.get("author") or None,
            )
        )
    return result
