import json
import xml.etree.ElementTree as ET
from collections.abc import Callable

from amir.publishing.sanitize import (
    escape_markdown,
    excerpt,
    neutralise_mentions,
    plain_text,
    truncate,
)


def test_steam_contents_become_clean_excerpt(captured: Callable[[str], bytes]) -> None:
    data = json.loads(captured("steam_getnewsforapp_announcements.json"))
    contents = data["appnews"]["newsitems"][0]["contents"]
    assert "{STEAM_CLAN_IMAGE}" in contents
    out = excerpt(plain_text(contents))
    assert "{STEAM_CLAN_IMAGE}" not in out
    assert "[" not in out
    assert 0 < len(out) <= 281


def test_forum_description_excerpted(captured: Callable[[str], bytes]) -> None:
    root = ET.fromstring(captured("forum_pc_update_notes.rss.xml"))
    desc = root.find(".//item/description")
    assert desc is not None and desc.text is not None
    assert len(desc.text) > 281 * 100  # a real patch note, far longer than an excerpt
    out = excerpt(plain_text(desc.text))
    assert "<" not in out
    assert 0 < len(out) <= 281


def test_hostile_text_is_neutralised_but_unicode_kept() -> None:
    out = escape_markdown(neutralise_mentions("@everyone <@&123> **x** Ñandú‏\U0001f409"))
    assert "@everyone" not in out
    assert "<@&" not in out
    assert "**" not in out
    assert "\\*\\*x\\*\\*" in out
    for kept in ("Ñandú", "\U0001f409", "‏"):
        assert kept in out


def test_neutralise_mentions_variants() -> None:
    out = neutralise_mentions("@here <@1> <@&2> <#3>")
    assert "@here" not in out
    for raw in ("<@", "<#"):
        assert raw not in out
    assert out.count("​") == 4


def test_plain_text_strips_html_entities_and_bbcode() -> None:
    raw = (
        "<p>Hi&nbsp;&amp; <b>bye</b></p>[h1]Title[/h1] [url=http://x]link[/url]"
        "\n\n  {STEAM_CLAN_IMAGE}/a/b.png end"
    )
    assert plain_text(raw) == "Hi & bye Title link end"


def test_plain_text_preserves_rlm_and_unicode() -> None:
    assert plain_text("a‏  b\n\tÑ\U0001f409") == "a‏ b Ñ\U0001f409"


def test_excerpt_short_unchanged_and_cut_at_word() -> None:
    assert excerpt("short text", 280) == "short text"
    out = excerpt("alpha beta gamma delta", 12)
    assert out == "alpha beta…"
    assert len(out) <= 13


def test_truncate() -> None:
    assert truncate("abc", 3) == "abc"
    out = truncate("abcdef", 4)
    assert out == "abc…" and len(out) == 4


def test_escape_markdown_chars_and_backslash_first() -> None:
    assert escape_markdown("\\*_~`|>") == "\\\\\\*\\_\\~\\`\\|\\>"
    assert escape_markdown("Ñ\U0001f409") == "Ñ\U0001f409"


def test_neutralise_mentions_no_word_boundary_and_case() -> None:
    for raw in ("@everyone_", "@everyone1", "@everyoneé", "@Everyone", "@HERE", "<@!1>"):
        out = neutralise_mentions(raw)
        assert out.startswith(("@\u200b", "<\u200b")), raw
        assert "@everyone" not in out.lower() and "@here" not in out.lower()
        assert "<@" not in out


def test_plain_text_decodes_entities_once() -> None:
    assert plain_text("&amp;lt;@1&amp;gt; &amp;amp;") == "&lt;@1&gt; &amp;"
