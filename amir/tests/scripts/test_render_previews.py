import html
import importlib.util
import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from amir.publishing.sanitize import escape_markdown
from amir.storage.repos import PreviewRepo

SCRIPT = Path(__file__).parents[2] / "scripts" / "render_previews.py"


@pytest.fixture(scope="module")
def rp():
    spec = importlib.util.spec_from_file_location("render_previews", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def built(rp):
    return rp.build_from_captures()


@pytest.fixture(scope="module")
def page(rp, built):
    cards, text, nows = built
    return rp.render(cards, text, now=rp.PREVIEW_NOW, note=rp.CAPTURE_NOTE, nows_by_section=nows)


def test_preview_page_from_real_data(rp, tmp_path):
    out = tmp_path / "cards.html"
    assert rp.main(["--out", str(out)]) == 0
    page = out.read_text(encoding="utf-8")
    text = html.unescape(page)
    for wanted in (
        "Iceblade of Narin: The Icebind: Hotfix 44.1.3",
        "Primed Fast Hands",
        "Strata Relay (Earth)",
        "Plains of Eidolon",
        "Orb Vallis",
        "Cambion Drift",
    ):
        assert wanted in text
    assert "<script" not in page
    assert "Eskhatos Ki'Teer Floof" not in text  # not a mod: counted in the summary line


def test_header_and_sections(page):
    text = html.unescape(page)
    assert "Previews only. Nothing has been posted to Discord." in text
    for heading in ("News", "Baro Ki'Teer", "Open-world cycles", "Voice log", "/amir news test"):
        assert heading in text
    assert "@member has arrived in #Warframe." in text
    assert "@member has left #Warframe. Safe travels." in text
    assert "Test: not a real announcement" in text
    assert "2026" in text and "EDT" in text


def test_no_discord_timestamps_left_unrendered(page):
    assert "<t:" not in html.unescape(page)


SUMMARY = "Also aboard: Skins and armour 28 · Ship decorations 7 · Other 4 · Glyphs 3 · Weapons 3"


def test_baro_arrival_lists_11_mods_and_one_summary_line(page, built):
    text = html.unescape(page)
    assert text.count(" ducats · ") == 11
    assert text.count(SUMMARY) == 1
    assert "Baro Ki'Teer is expected" in text
    assert "Last call for Baro Ki'Teer" in text
    cards, *_ = built
    _, arrival, _ = cards["Baro Ki'Teer"]
    # Opening, the list, the summary: three parts, set apart by blank lines.
    opening, listed, summary = arrival["description"].split("\n\n")
    heading, *mods = listed.split("\n")
    assert opening.startswith("He keeps shop until ")
    assert heading == "His wares, as listed:"
    assert len(mods) == 11
    assert all(" ducats · " in line for line in mods)
    assert summary == SUMMARY


def test_news_has_forum_card_and_three_steam_cards(built):
    cards, *_ = built
    news = cards["News"]
    assert len(news) == 4
    assert [c["footer"]["text"] for c in news].count(
        "Amir Beckett · Official Steam announcements"
    ) == 3


def test_page_is_self_contained(page):
    assert "<script" not in page
    assert not re.search(r"""(src|href)\s*=\s*["']https?:""", page)
    assert "<img" not in page and "@import" not in page


def test_hostile_title_is_escaped(rp, built):
    cards, text, _ = built
    hostile = dict(cards["News"][0])
    hostile["title"] = "@everyone <script>" + hostile["title"]
    hostile["description"] = "<script>alert(1)</script> **b** " + hostile["description"]
    out = rp.render({"News": [hostile]}, {}, now=rp.PREVIEW_NOW)
    assert "<script" not in out
    assert "&lt;script&gt;" in out


def test_markdown_bold_and_escapes(rp, built):
    cards, *_ = built
    stock = next(c for c in cards["Baro Ki'Teer"] if " ducats · " in c["description"])
    real_line = [x for x in stock["description"].splitlines() if " ducats · " in x][-1]
    line = real_line.replace("**", "**" + escape_markdown("a*b_c"), 1)
    assert "\\*" in line
    out = rp.render({"S": [{"title": "t", "description": line}]}, {}, now=rp.PREVIEW_NOW)
    assert "<strong>a*b_c" in out and "**" not in out
    raw = {"title": "t", "description": "<b>" + real_line}
    assert "&lt;b&gt;" in rp.render({"S": [raw]}, {}, now=rp.PREVIEW_NOW)


def test_discord_timestamp_styles(rp, built):
    cards, *_ = built
    cycle = cards["Open-world cycles"][0]
    unix = int(re.search(r"<t:(\d+):R>", cycle["description"])[1])
    local = datetime.fromtimestamp(unix, tz=ZoneInfo("America/New_York"))
    out = html.unescape(rp.render({"S": [cycle]}, {}, now=rp.PREVIEW_NOW))
    assert "<t:" not in out
    assert local.strftime("%I:%M %p").lstrip("0") in out
    delta = unix - int(rp.PREVIEW_NOW.timestamp())
    amount, unit = (delta // 3600, "hour") if delta >= 3600 else (delta // 60, "minute")
    assert f"in {amount} {unit}{'' if amount == 1 else 's'}" in out
    warning = cards["Baro Ki'Teer"][0]
    activation = int(re.search(r"<t:(\d+):F>", warning["description"])[1])
    baro = html.unescape(rp.render({"S": [warning]}, {}, now=rp.PREVIEW_NOW))
    when = datetime.fromtimestamp(activation, tz=ZoneInfo("America/New_York"))
    assert (
        when.strftime("%A, %B ")
        + str(when.day)
        + when.strftime(", %Y %I:%M %p %Z").replace(" 0", " ", 1)
        in baro
    )
    days = (int(rp.PREVIEW_NOW.timestamp()) - activation) // 86400
    assert f"{days} day{'' if days == 1 else 's'} ago" in baro


async def test_from_db_renders_previews_read_only(rp, db, clock, built, tmp_path):
    cards, *_ = built
    repo = PreviewRepo(db, clock)
    await repo.add("news", "send", {"cards": [cards["News"][0]], "text": None})
    await repo.add("voice_log", "send", {"cards": [], "text": "@member has arrived in #Warframe."})
    db_file = next(tmp_path.glob("*.sqlite3"))
    snapshot = db_file.read_bytes()
    out = tmp_path / "db.html"
    assert rp.main(["--from-db", str(db_file), "--out", str(out)]) == 0
    text = html.unescape(out.read_text(encoding="utf-8"))
    assert "Iceblade of Narin: The Icebind: Hotfix 44.1.3" in text
    assert "@member has arrived in #Warframe." in text
    assert "news" in text and "voice_log" in text
    assert "<script" not in out.read_text(encoding="utf-8")
    assert db_file.read_bytes() == snapshot


def test_from_db_connection_is_read_only(rp, tmp_path):
    path = tmp_path / "x.sqlite3"
    sqlite3.connect(path).close()
    conn = rp.open_read_only(path)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("CREATE TABLE t (a)")


def test_from_db_missing_file_fails_cleanly(rp, tmp_path, capsys):
    assert (
        rp.main(["--from-db", str(tmp_path / "nope.sqlite3"), "--out", str(tmp_path / "o.html")])
        == 1
    )
    assert "nope.sqlite3" in capsys.readouterr().err
    assert not (tmp_path / "o.html").exists()


def test_news_test_card_reused(rp, built):
    from amir.bot.commands import news_test_card

    cards, *_ = built
    assert cards["/amir news test"] == [news_test_card().to_dict()]


def test_baro_cards_read_as_at_their_own_milestone(rp, built):
    cards, _, nows = built
    baro = cards["Baro Ki'Teer"]
    assert len(nows["Baro Ki'Teer"]) == len(baro)

    def card_text(i):
        out = rp.render({"S": [baro[i]]}, {}, now=nows["Baro Ki'Teer"][i])
        return html.unescape(out)

    assert "in 3 days" in card_text(0)
    assert "in 2 days" in card_text(1)
    assert "in 10 hours" in card_text(len(baro) - 1)
    assert all("ago" not in card_text(i) for i in range(len(baro)))


def test_page_baro_section_has_no_ago(page):
    assert "in 3 days" in page and "in 10 hours" in page
    assert "ago" not in re.sub(r"<[^>]+>", "", page.split("<h2>Baro")[1].split("<h2>Open")[0])


def test_capture_note_is_derived_from_preview_now(rp):
    assert "2026-10-24 21:21 EDT" in rp.CAPTURE_NOTE


def test_inline_and_block_fields_get_distinct_classes(rp, built):
    cards, *_ = built
    card = dict(cards["News"][0])
    assert [f["inline"] for f in card["fields"]] == [False, False]
    card["fields"] = [card["fields"][0], {**card["fields"][1], "inline": True}]
    out = rp.render({"S": [card]}, {}, now=rp.PREVIEW_NOW)
    assert 'class="field block"' in out and 'class="field inline"' in out
    assert ".field.block{flex:0 0 100%}" in out
    assert "calc((100% - 48px) / 3)" in out


def test_url_line_is_labelled(rp, built):
    cards, *_ = built
    out = rp.render({"S": [cards["News"][0]]}, {}, now=rp.PREVIEW_NOW)
    assert "title links to: https://forums.warframe.com/topic/" in out


def test_hostile_card_fields_are_all_escaped(rp, built):
    cards, *_ = built
    evil = '"><script>alert(1)</script>'
    card = dict(cards["News"][0])
    card["url"] = "javascript:" + evil
    card["fields"] = [{**f, "name": evil + f["name"], "value": evil} for f in card["fields"]]
    card["footer"] = {"text": evil}
    card["thumbnail"] = {"url": "javascript:" + evil}
    out = rp.render({evil: [card]}, {evil: [evil]}, now=rp.PREVIEW_NOW)
    assert "<script" not in out
    assert "href" not in out
    assert out.count("&lt;script&gt;") >= 7


async def test_from_db_labels_edits_and_uses_each_rows_own_time(
    rp, db, make_clock, built, tmp_path
):
    cards, *_ = built
    warning = cards["Baro Ki'Teer"][0]
    activation = int(re.search(r"<t:(\d+):F>", warning["description"])[1])
    posted = datetime.fromtimestamp(activation, tz=UTC) - timedelta(hours=72)
    await PreviewRepo(db, make_clock(posted)).add("news", "send", {"cards": [warning]})
    await PreviewRepo(db, make_clock(posted + timedelta(hours=1))).add(
        "cycles", "edit", {"cards": [cards["Open-world cycles"][0]]}
    )
    await PreviewRepo(db, make_clock(posted)).add("voice_log", "send", {"text": "@member x"})
    db_file = next(tmp_path.glob("*.sqlite3"))
    out = tmp_path / "db.html"
    assert rp.main(["--from-db", str(db_file), "--out", str(out)]) == 0
    text = html.unescape(out.read_text(encoding="utf-8"))
    news = text.split("#news")[1].split("#cycles")[0]
    assert "New message" in news and "in 3 days" in news
    cycles = text.split("#cycles")[1]
    assert "Edit of an earlier message" in cycles
    assert "would have been posted" in cycles
    assert "New message" in text.split("#voice_log")[1]


async def test_from_db_skips_unusable_rows(rp, db, clock, built, tmp_path, capsys):
    cards, *_ = built
    repo = PreviewRepo(db, clock)
    for _ in range(3):
        await repo.add("news", "send", {"cards": [cards["News"][0]]})
    db_file = next(tmp_path.glob("*.sqlite3"))
    with sqlite3.connect(db_file) as conn:
        conn.execute("UPDATE previews SET payload_json = 'not json {' WHERE id = 1")
        conn.execute("UPDATE previews SET payload_json = '[1, 2]' WHERE id = 2")
    out = tmp_path / "db.html"
    assert rp.main(["--from-db", str(db_file), "--out", str(out)]) == 0
    err = capsys.readouterr().err
    assert "row 1" in err and "row 2" in err and "row 3" not in err
    assert "Traceback" not in err
    assert html.unescape(out.read_text(encoding="utf-8")).count("Iceblade of Narin") == 1


async def test_out_may_not_be_the_database(rp, db, clock, built, tmp_path, capsys):
    cards, *_ = built
    await PreviewRepo(db, clock).add("news", "send", {"cards": [cards["News"][0]]})
    db_file = next(tmp_path.glob("*.sqlite3"))
    snapshot = db_file.read_bytes()
    assert rp.main(["--from-db", str(db_file), "--out", str(db_file)]) != 0
    assert "refusing" in capsys.readouterr().err
    assert db_file.read_bytes() == snapshot


async def test_from_db_keeps_news_and_baro_after_many_cycle_rows(rp, db, clock, built, tmp_path):
    cards, *_ = built
    news, warning = cards["News"][0], cards["Baro Ki'Teer"][0]
    repo = PreviewRepo(db, clock)
    await repo.add("news", "send", {"cards": [news]})
    await repo.add("news", "send", {"cards": [warning]})
    for _ in range(rp.DB_LIMIT + 10):  # a gated run previews every cycle flip
        clock.advance(60)
        await repo.add("cycles", "edit", {"cards": [cards["Open-world cycles"][0]]})
    db_file = next(tmp_path.glob("*.sqlite3"))

    shown, *_ = rp.build_from_db(db_file)

    assert shown["#news"] == [warning, news]  # newest first
    assert len(shown["#cycles"]) == rp.DB_LIMIT
    out = tmp_path / "db.html"
    assert rp.main(["--from-db", str(db_file), "--out", str(out)]) == 0
    section = html.unescape(out.read_text(encoding="utf-8")).split("#news")[1].split("#cycles")[0]
    assert "Iceblade of Narin: The Icebind: Hotfix 44.1.3" in section
    assert "Baro Ki'Teer is expected" in section
