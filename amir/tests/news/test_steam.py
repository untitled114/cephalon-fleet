import copy
import json
from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from amir.news.canonical import canonicalize_url
from amir.news.filtering import exclusion_reason
from amir.news.steam import FeedParseError, parse_steam_news

CAPTURE = "steam_getnewsforapp_announcements.json"
KEYWORDS = [
    "baro ki'teer",
    "soundtrack",
    "single release",
    "tennogen",
    "battlekit",
    "bundle",
    "merch",
    "plush",
]
EXPECTED = {
    '"Devourer" Single Release': "single release",
    "Baro Ki'Teer Returns": "baro ki'teer",
    "Iceblade of Narin Official Soundtrack": "soundtrack",
    "TennoGen Iceblade Available Now": "tennogen",
    "Glaxion Battlekit Available Now": "battlekit",
    "Reinforce your Arsenal with Iceblade of Narin Bundles": "bundle",
}


def _derived(captured: Callable[[str], bytes], mutate: Callable[[dict], None]) -> bytes:
    data = json.loads(captured(CAPTURE))
    mutate(data["appnews"]["newsitems"][0])
    return json.dumps(data).encode()


def test_real_steam_feed(captured: Callable[[str], bytes]) -> None:
    result = parse_steam_news(captured(CAPTURE), "steam")
    assert len(result.items) == 20
    assert len({i.external_id for i in result.items}) == 20
    first = result.items[0]
    assert first.title == '"Devourer" Single Release'
    assert first.published_at == datetime(2026, 10, 19, 15, 12, 23, tzinfo=UTC)
    assert first.category == "Announcement"
    assert first.author == "elizabeth.yuen"
    assert first.source_id == "steam"
    assert all(i.canonical_url == canonicalize_url(i.url) for i in result.items)
    assert all(i.excerpt for i in result.items)


def test_third_party_items_dropped(captured: Callable[[str], bytes]) -> None:
    data = json.loads(captured(CAPTURE))
    extra = copy.deepcopy(data["appnews"]["newsitems"][0])
    extra["feedname"] = "PCGamesN"
    extra["gid"] = "third-party-" + extra["gid"]
    data["appnews"]["newsitems"].append(extra)
    result = parse_steam_news(json.dumps(data).encode(), "steam")
    assert len(result.items) == 20
    assert all(not i.external_id.startswith("third-party-") for i in result.items)


def test_default_exclusions_on_real_titles(captured: Callable[[str], bytes]) -> None:
    result = parse_steam_news(captured(CAPTURE), "steam")
    reasons = {i.title: exclusion_reason(i.title, KEYWORDS) for i in result.items}
    assert {t: r for t, r in reasons.items() if r} == EXPECTED
    kept = [t for t, r in reasons.items() if r is None]
    assert len(kept) == 14
    assert "Devstream #198 - Tau Part One!" in kept
    assert "Warframe’s Halloween 2026 Events" in kept


def test_not_json_rejected() -> None:
    with pytest.raises(FeedParseError):
        parse_steam_news(b"<html>maintenance</html>", "steam")


@pytest.mark.parametrize("body", [b"{}", b'{"appnews": {}}', b"[1]", b'{"appnews": []}'])
def test_missing_appnews_rejected(body: bytes) -> None:
    with pytest.raises(FeedParseError):
        parse_steam_news(body, "steam")


def test_missing_gid_skipped(captured: Callable[[str], bytes]) -> None:
    result = parse_steam_news(_derived(captured, lambda i: i.pop("gid")), "steam")
    assert len(result.items) == 19
    assert result.problems


def test_missing_title_skipped(captured: Callable[[str], bytes]) -> None:
    result = parse_steam_news(_derived(captured, lambda i: i.pop("title")), "steam")
    assert len(result.items) == 19
    assert result.problems


@pytest.mark.parametrize("bad", ["soon", None])
def test_bad_date_tolerated(captured: Callable[[str], bytes], bad: object) -> None:
    result = parse_steam_news(_derived(captured, lambda i: i.update(date=bad)), "steam")
    assert len(result.items) == 20
    assert result.items[0].published_at is None
    assert result.problems


def test_absent_date_tolerated(captured: Callable[[str], bytes]) -> None:
    result = parse_steam_news(_derived(captured, lambda i: i.pop("date")), "steam")
    assert result.items[0].published_at is None
    assert result.problems


def test_empty_author_is_none(captured: Callable[[str], bytes]) -> None:
    result = parse_steam_news(_derived(captured, lambda i: i.update(author="")), "steam")
    assert result.items[0].author is None
