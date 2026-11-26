from collections.abc import Callable

from amir.news.cards import ANNOUNCEMENT_COLOR, HOTFIX_COLOR, UPDATE_COLOR, news_card
from amir.news.forum_rss import parse_forum_rss
from amir.news.steam import parse_steam_news
from amir.persona import footer
from amir.publishing.cards import Card

FORUM_LABEL = "Official forums · PC Update Notes"
STEAM_LABEL = "Official Steam announcements"


def _forum(captured: Callable[[str], bytes]):
    return parse_forum_rss(captured("forum_pc_update_notes.rss.xml"), "forum-pc-updates").items


def _steam(captured: Callable[[str], bytes]):
    return parse_steam_news(
        captured("steam_getnewsforapp_announcements.json"), "steam-announcements"
    ).items


def test_forum_card_from_real_item(captured: Callable[[str], bytes]) -> None:
    item = _forum(captured)[0]
    card = news_card(item, source_label=FORUM_LABEL)
    assert card.title == "Iceblade of Narin: The Icebind: Hotfix 44.1.3"
    assert card.url is not None and card.url.startswith("https://forums.warframe.com/")
    assert card.url == item.canonical_url
    assert {f.name: f.value for f in card.fields} == {
        "Category": "Hotfix",
        "Source": FORUM_LABEL,
    }
    assert card.description == "\n".join(
        [
            "**Arsenal Retuning Inbox Message**",
            "This hotfix brings changes to Ammo Efficiency, Riven Mods, and hot-off-the-presses"
            " Acid Shell interactions, so all players who log in after today’s update will"
            " receive the following (until December 31st 2026 at 11:59pm ET):",
            "• 2 Built Forma + 100,000 Kuva",
            "• NOTE: To ensure all players receive the inbox as intended, it will be delivered"
            " at 3:30pm ET.",
            "Players who have installed a Riven Splicer in a Riven Mod prior to today’s hotfix"
            " will also receive the following in a separate inbox as a thank you for being an"
            " early adopter:",
            "• 1 Riven Splicer + 100,000 Kuva",
            "• NOTE: This inbox requires a script to run, which we will do shortly after the"
            " hotfix launches.",
            "…",
        ]
    )
    assert card.timestamp == item.published_at
    assert card.color == HOTFIX_COLOR
    assert card.footer == "Amir Beckett · Official forums · PC Update Notes"


def test_steam_card_keeps_unicode_title(captured: Callable[[str], bytes]) -> None:
    item = next(i for i in _steam(captured) if "Halloween" in i.title)
    card = news_card(item, source_label=STEAM_LABEL)
    assert card.title == "Warframe’s Halloween 2026 Events"
    assert card.color == ANNOUNCEMENT_COLOR
    assert dict((f.name, f.value) for f in card.fields)["Category"] == "Announcement"


def test_card_has_no_timestamp_markup_in_title_or_footer(
    captured: Callable[[str], bytes],
) -> None:
    for item, label in [(_forum(captured)[0], FORUM_LABEL), (_steam(captured)[0], STEAM_LABEL)]:
        card = news_card(item, source_label=label)
        assert "<t:" not in card.title
        assert "<t:" not in (card.footer or "")


def test_hostile_title_and_description_cannot_ping(captured: Callable[[str], bytes]) -> None:
    from dataclasses import replace

    item = replace(_forum(captured)[0], title="Hi @everyone <@&123>", excerpt="cc @here")
    card = news_card(item, source_label=FORUM_LABEL)
    assert "@everyone" not in card.title.replace("@​everyone", "")
    assert "<@&123>" not in card.title
    assert "@here" not in card.description.replace("@​here", "")


def test_category_colours_and_unknown_fallback(captured: Callable[[str], bytes]) -> None:
    from dataclasses import replace

    base = _forum(captured)[0]
    assert news_card(replace(base, category="Update"), source_label="x").color == UPDATE_COLOR
    assert len({HOTFIX_COLOR, UPDATE_COLOR, ANNOUNCEMENT_COLOR}) == 3
    unknown = news_card(replace(base, category="Weird"), source_label="x")
    assert unknown.color == Card(title="t").color


def test_footer_is_amir_and_never_de() -> None:
    assert footer("Official forums · PC Update Notes") == (
        "Amir Beckett · Official forums · PC Update Notes"
    )
    assert "Digital Extremes" not in footer("x")
