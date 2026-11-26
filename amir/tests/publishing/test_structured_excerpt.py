from pathlib import Path

import feedparser
import pytest

from amir.news.forum_rss import parse_forum_rss
from amir.publishing.sanitize import structured_excerpt

CAPTURES = Path(__file__).parent.parent / "fixtures" / "captured"
FEED = "forum_pc_update_notes.rss.xml"
NBSP4 = " " * 4


def _entries(day: str) -> list[feedparser.FeedParserDict]:
    return feedparser.parse((CAPTURES / day / FEED).read_bytes()).entries


def _summary(day: str, title_ends: str) -> str:
    entry = next(e for e in _entries(day) if e.title.endswith(title_ends))
    return entry.summary


def _lines(text: str) -> list[str]:
    return text.split("\n")


def test_hotfix_44_1_4_is_exactly_the_approved_text() -> None:
    rooms = (
        "Duviri Cave",
        "Earth Forest Chamber",
        "Galleon Chamber",
        "Infested Chamber",
        "Kuva Asteroid",
        "Orokin Tower Chamber",
        "Ostron Cove",
        "Starlight Market",
        "Uranus Chamber",
    )
    expected = "\n".join(
        [
            "**Changes**",
            "• Reduced the Nami Skyla Prime Blade's Ducat value from 100 to 65 as a result of its"
            " varied rarity from Void Relics.",
            "• The Skull Shots Augment (Viper) can now retrigger its buff while it is currently"
            " active.",
            '• Added the option "Make Spawn Room" to the following Dojo rooms (at room console)',
            *[f"{NBSP4}◦ {room}" for room in rooms],
            "",
            "**Fixes**",
            "• Fixed cases of The Icebind Mobile Defense missions not properly extracting due to a"
            " specific tile in the Grineer Asteroid tileset.",
            "• Fixed players who have not unlocked Eris being unable to see or play the Epoch"
            " Operation: Taubound Alerts.",
            "• Fixed Amanata Pressure not applying to Heavy Slam Attacks.",
            f"{NBSP4}◦ Also fixed Amanata Pressure only affecting one enemy per Heavy Attack.",
            "…",
        ]
    )
    assert structured_excerpt(_summary("2026-10-27", "Hotfix 44.1.4")) == expected
    assert len(expected) <= 900


def test_forum_parser_uses_the_structured_excerpt() -> None:
    data = (CAPTURES / "2026-10-27" / FEED).read_bytes()
    items = parse_forum_rss(data, "forum").items
    item = next(i for i in items if i.title.endswith("Hotfix 44.1.4"))
    assert item.excerpt.startswith(
        "**Changes**\n• Reduced the Nami Skyla Prime Blade's Ducat value from 100 to 65"
    )
    assert item.excerpt.endswith(
        "\n**Fixes**\n• Fixed cases of The Icebind Mobile Defense missions not properly"
        " extracting due to a specific tile in the Grineer Asteroid tileset.\n"
        "• Fixed players who have not unlocked Eris being unable to see or play the Epoch"
        " Operation: Taubound Alerts.\n"
        "• Fixed Amanata Pressure not applying to Heavy Slam Attacks.\n"
        f"{NBSP4}◦ Also fixed Amanata Pressure only affecting one enemy per Heavy Attack.\n…"
    )


def test_hotfix_44_1_3_heading_paragraph_then_list() -> None:
    html = _summary("2026-10-24", "Hotfix 44.1.3")
    out = structured_excerpt(html)
    lines = _lines(out)
    assert lines[0] == "**Arsenal Retuning Inbox Message**"
    assert lines[1].startswith("This hotfix brings changes to Ammo Efficiency, Riven Mods,")
    assert lines[1].endswith("(until December 31st 2026 at 11:59pm ET):")
    assert lines[2] == "• 2 Built Forma + 100,000 Kuva"
    assert len(out) <= 900
    assert lines[-1] == "…"
    full = set(_lines(structured_excerpt(html, limit=10**9)))
    assert all(line in full for line in lines[:-1])


def test_headings_lose_one_colon_and_italic_blocks_drop() -> None:
    full = structured_excerpt(_summary("2026-10-27", "Hotfix 44.1.4"), limit=10**9)
    assert "\n\n**Script Error Fixes**\n• Fixed a script error related to transmissions" in full
    assert "Script errors mean that something went wrong" not in full
    assert "&amp;" not in full and "Sirius & Orion" in full


def test_hotfix_44_1_2_has_no_image_description_or_filename() -> None:
    out = structured_excerpt(_summary("2026-10-24", "Hotfix 44.1.2 + 2.1"), limit=10**9)
    assert "Image Description" not in out
    assert ".png" not in out and "7f61d1cc" not in out
    assert out.startswith("**Iceblade of Narin: The Icebind: Hotfix #2.1**\n\n**Fixes**\n")
    assert "\n\n**Tau Prologue Quest Series**\nA series of new Prologue Quests" in out


def test_short_post_has_no_ellipsis() -> None:
    shorts = []
    for day in ("2026-10-24", "2026-10-27"):
        for entry in _entries(day):
            full = structured_excerpt(entry.summary, limit=10**9)
            if 0 < len(full) <= 900:
                shorts.append((entry.summary, full))
    assert shorts
    for html, full in shorts:
        assert structured_excerpt(html) == full
        assert not full.endswith("…")


def test_first_line_alone_over_the_limit_is_cut_at_a_space() -> None:
    html = _summary("2026-10-27", "Hotfix 44.1.4")
    from_list = html[html.index("<ul>") :]
    text = "Reduced the Nami Skyla Prime Blade's Ducat value from 100 to 65"
    start = from_list.index(text)
    end = from_list.index("</li>", start)
    original = from_list[start:end].strip()
    long_html = from_list[:start] + " ".join([original] * 12) + from_list[end:]
    out = structured_excerpt(long_html)
    assert out.startswith("• Reduced the Nami Skyla")
    assert out.endswith("…") and "\n" not in out
    assert len(out) <= 901
    assert out[:-1].rstrip() == out[:-1]
    assert (" ".join([original] * 12)).startswith(out[2:-1])


@pytest.mark.parametrize("limit", [200, 500, 900])
def test_never_cuts_inside_a_line(limit: int) -> None:
    html = _summary("2026-10-24", "Hotfix 44.1.3")
    full = set(_lines(structured_excerpt(html, limit=10**9)))
    out = structured_excerpt(html, limit=limit)
    assert len(out) <= limit
    assert all(line in full for line in _lines(out)[:-1])


def test_excerpt_never_ends_on_a_heading_without_content() -> None:
    out = structured_excerpt(_summary("2026-10-24", "Hotfix 44.1.3"))
    lines = _lines(out)
    assert lines[-1] == "…"
    before = lines[-2]
    assert before.strip() != ""
    assert not (before.startswith("**") and before.endswith("**"))
    assert before.startswith("• NOTE: This inbox requires a script to run")


@pytest.mark.parametrize(
    ("title_starts", "line"),
    [
        (
            "Iceblade of Narin: Hotfix 44.0.1",
            "We hope this clears up confusion around how to best approach taking on Entropic"
            " Eximus. If you’re hunting them down, don’t forget to check Void Angels for a few"
            " guaranteed spawns! To provide us feedback on Entropic Eximus, please head over to"
            " our Update 44 Zariman Updates Feedback Forum.",
        ),
        (
            "Jade Shadows: Constellations: Hotfix 43.0.2",
            f"{NBSP4}◦ Known Issue: the Drifter's right arm now turns invisible when using these"
            " Emotes. Fixing nightmare fuel apparently comes with a cost.",
        ),
        (
            "Update 44.1.0",
            "Players who are invited to a Squad will be summoned to Yuvan Peak, where they can"
            " then gather their Cryobells and proceed to Cephalon Melica. All players must speak"
            " to Cephalon Melica and select “Ring a Cryobell” to start the mission.",
        ),
    ],
)
def test_inline_tags_add_no_spaces_before_punctuation(title_starts: str, line: str) -> None:
    entry = next(e for e in _entries("2026-10-24") if e.title.startswith(title_starts))
    assert line in _lines(structured_excerpt(entry.summary, limit=10**9))
