"""Render Amir's cards as a self-contained HTML page for owner approval, outside Discord.

Usage: uv run python scripts/render_previews.py [--out PATH] [--from-db DB]

Without --from-db the cards are built from the real 2026-10-24 captures. With --from-db, the
`previews` table of a copy of the server database is rendered instead (what Amir would have
posted while gated); the file is opened read-only and never written. Nothing is sent anywhere.
"""

import argparse
import html
import json
import re
import sqlite3
import sys
import tomllib
from collections import defaultdict
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TextIO
from urllib.parse import quote
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from amir import persona  # noqa: E402
from amir.baro.cards import (  # noqa: E402
    arrival_stock,
    baro_arrival_messages,
    baro_departure_card,
    baro_warning_card,
    relay_name,
)
from amir.baro.names import ItemNameIndex  # noqa: E402
from amir.bot.commands import news_test_card  # noqa: E402
from amir.cycles.cards import cycle_card  # noqa: E402
from amir.cycles.model import roll_forward  # noqa: E402
from amir.news.cards import news_card  # noqa: E402
from amir.news.filtering import exclusion_reason  # noqa: E402
from amir.news.forum_rss import parse_forum_rss  # noqa: E402
from amir.news.steam import parse_steam_news  # noqa: E402
from amir.worldstate.official import parse_void_trader  # noqa: E402
from amir.worldstate.wfcd import (  # noqa: E402
    parse_baro_location,
    parse_cycles,
    parse_inventory_names,
    parse_item_names,
)

NY = ZoneInfo("America/New_York")
CAPTURED = ROOT / "tests" / "fixtures" / "captured" / "2026-10-24"
CONFIG = ROOT / "config" / "amir.example.toml"
DEFAULT_OUT = ROOT / "docs" / "previews" / "cards.html"
PREVIEW_NOW = datetime(2026, 10, 25, 1, 21, tzinfo=UTC)
CAPTURE_NOTE = (
    f"Real data captured {PREVIEW_NOW.astimezone(NY):%Y-%m-%d %H:%M %Z} (Baro Ki'Teer present)."
)
STEAM_CARDS = 3
DB_LIMIT = 200  # newest rows rendered per channel, so cycle edits never crowd out news
HEADER = "Previews only. Nothing has been posted to Discord."

# ---------------------------------------------------------------- building from captures


def _sources(config: Mapping) -> dict[str, str]:
    return {s["id"]: s["label"] for s in config["news"]["sources"]}


def build_from_captures(
    captured: Path = CAPTURED, config_path: Path = CONFIG, now: datetime = PREVIEW_NOW
) -> tuple[dict[str, list[dict]], dict[str, list[str]], dict[str, list[datetime]]]:
    """Cards, text, and per-card "now" for sections whose cards post at their own moment."""
    config = tomllib.loads(config_path.read_text())
    labels = _sources(config)
    keywords = config["news"]["exclude_title_keywords"]

    forum = parse_forum_rss(
        (captured / "forum_pc_update_notes.rss.xml").read_bytes(), "forum-pc-updates"
    ).items
    newest = max((i for i in forum if i.published_at), key=lambda i: i.published_at)
    steam = parse_steam_news(
        (captured / "steam_getnewsforapp_announcements.json").read_bytes(), "steam-announcements"
    ).items
    kept = [i for i in steam if exclusion_reason(i.title, keywords) is None][:STEAM_CARDS]
    news = [news_card(newest, source_label=labels["forum-pc-updates"]).to_dict()]
    news += [news_card(i, source_label=labels["steam-announcements"]).to_dict() for i in kept]

    wfcd = (captured / "wfcd_pc_baro_present.json").read_bytes()
    visit = parse_void_trader((captured / "official_worldstate_baro_present.json").read_bytes())
    if visit is None:
        raise SystemExit("the capture holds no Baro visit")
    location = relay_name(visit.node, parse_baro_location(wfcd), visit.activation)
    index = ItemNameIndex(parse_item_names((captured / "wfcd_items_names.json").read_bytes()))
    stock = arrival_stock(visit, index, parse_inventory_names(wfcd))
    arrival = [c for msg in baro_arrival_messages(visit, location, stock) for c in msg]
    baro = [baro_warning_card(visit, location), *arrival, baro_departure_card(visit, location)]

    # Each Baro card is posted at its own milestone, so its relative times read as then.
    warn_hours = config["baro"]
    nows = {
        "Baro Ki'Teer": [
            visit.activation - timedelta(hours=warn_hours["warn_before_arrival_hours"]),
            *[visit.activation] * len(arrival),
            visit.expiry - timedelta(hours=warn_hours["warn_before_departure_hours"]),
        ]
    }

    durations = config["cycles"]["durations"]
    cycles = [
        cycle_card(roll_forward(snap, now, durations)).to_dict()
        for snap in parse_cycles(wfcd).values()
    ]

    cards = {
        "News": news,
        "Baro Ki'Teer": [c.to_dict() for c in baro],
        "Open-world cycles": cycles,
        "/amir news test": [news_test_card().to_dict()],
    }
    text = {
        "Voice log": [
            persona.voice_join_line("@member", "#Warframe"),
            persona.voice_leave_line("@member", "#Warframe"),
        ]
    }
    return cards, text, nows


def open_read_only(path: Path) -> sqlite3.Connection:
    uri = f"file:{quote(str(Path(path).resolve()))}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def build_from_db(
    path: Path, err: TextIO | None = None
) -> tuple[
    dict[str, list[dict]],
    dict[str, list[str]],
    dict[str, list[datetime]],
    dict[str, list[str]],
]:
    """Cards, text, and for each section's items (cards, then text) a "now" and a caption."""
    err = err or sys.stderr
    conn = open_read_only(path)
    try:
        rows = conn.execute(
            "SELECT id, created_at, channel, action, payload_json FROM ("
            " SELECT *, ROW_NUMBER() OVER (PARTITION BY channel ORDER BY id DESC) AS n"
            " FROM previews) WHERE n <= ? ORDER BY id DESC",
            (DB_LIMIT,),
        ).fetchall()
    finally:
        conn.close()
    cards: dict[str, list[dict]] = defaultdict(list)
    text: dict[str, list[str]] = defaultdict(list)
    card_meta: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
    text_meta: dict[str, list[tuple[datetime, str]]] = defaultdict(list)
    for row in rows:
        try:
            payload = json.loads(row["payload_json"])
            created = datetime.fromisoformat(row["created_at"])
            if not isinstance(payload, dict) or created.tzinfo is None:
                raise ValueError("unusable row")
        except (ValueError, TypeError):
            print(f"skipped preview row {row['id']}: unusable payload or time", file=err)
            continue
        section = f"#{row['channel']}"
        kind = "Edit of an earlier message" if row["action"] == "edit" else "New message"
        caption = (
            f"{kind} · would have been posted {_timestamp(int(created.timestamp()), 'f', created)}"
        )
        found = [c for c in payload.get("cards") or [] if isinstance(c, dict)]
        cards[section].extend(found)
        card_meta[section].extend((created, caption) for _ in found)
        if payload.get("text"):
            text[section].append(str(payload["text"]))
            text_meta[section].append((created, caption))
    nows: dict[str, list[datetime]] = {}
    captions: dict[str, list[str]] = {}
    for section in {*card_meta, *text_meta}:
        meta = [*card_meta[section], *text_meta[section]]
        nows[section] = [m[0] for m in meta]
        captions[section] = [m[1] for m in meta]
    return dict(cards), dict(text), nows, captions


# ---------------------------------------------------------------- rendering

_TOKEN = re.compile(
    r"<t:(?P<unix>-?\d+)(?::(?P<style>[a-zA-Z]))?>"
    r"|\\(?P<esc>[\\*_~`|>#\[\]()-])"
    r"|\*\*(?P<bold>.+?)(?<!\\)\*\*"
    r"|(?P<nl>\n)",
    re.DOTALL,
)


def _plural(n: int, unit: str) -> str:
    return f"{n} {unit}{'' if n == 1 else 's'}"


def _relative(then: datetime, now: datetime) -> str:
    seconds = int((then - now).total_seconds())
    span = abs(seconds)
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute"), (1, "second")):
        if span >= size or unit == "second":
            amount = _plural(span // size, unit)
            break
    return f"in {amount}" if seconds >= 0 else f"{amount} ago"


def _clock(dt: datetime) -> str:
    return dt.strftime("%I:%M %p").lstrip("0")


def _timestamp(unix: int, style: str, now: datetime) -> str:
    try:
        then = datetime.fromtimestamp(unix, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return "(invalid time)"
    local = then.astimezone(NY)
    zone = local.strftime("%Z")
    day = f"{local:%B} {local.day}, {local.year}"
    if style == "R":
        return _relative(then, now)
    if style == "t":
        return _clock(local)
    if style == "T":
        return f"{_clock(local)[:-3]}:{local:%S} {local:%p}"
    if style == "d":
        return f"{local:%m/%d/%Y}"
    if style == "D":
        return day
    if style == "f":
        return f"{day} {_clock(local)} {zone}"
    return f"{local:%A}, {day} {_clock(local)} {zone}"


def _markup(raw: str, now: datetime) -> str:
    """Discord markdown (bold, backslash escapes, newlines) and timestamps, as safe HTML."""
    out: list[str] = []
    pos = 0
    for m in _TOKEN.finditer(raw):
        out.append(html.escape(raw[pos : m.start()]))
        pos = m.end()
        if m["unix"] is not None:
            text = _timestamp(int(m["unix"]), m["style"] or "f", now)
            out.append(f'<span class="ts">{html.escape(text)}</span>')
        elif m["esc"] is not None:
            out.append(html.escape(m["esc"]))
        elif m["bold"] is not None:
            out.append(f"<strong>{_markup(m['bold'], now)}</strong>")
        else:
            out.append("<br>")
    out.append(html.escape(raw[pos:]))
    return "".join(out)


def _as_text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _card_html(card: Mapping, now: datetime) -> str:
    color = card.get("color")
    color = color if isinstance(color, int) and 0 <= color <= 0xFFFFFF else 0x4F545C
    parts = [f'<div class="embed" style="border-left-color:#{color:06x}">']
    title = _markup(_as_text(card.get("title")), now)
    url = _as_text(card.get("url"))
    parts.append(f'<div class="etitle">{title}</div>')
    if url:
        parts.append(f'<div class="eurl">title links to: {html.escape(url)}</div>')
    if card.get("description"):
        parts.append(f'<div class="edesc">{_markup(_as_text(card["description"]), now)}</div>')
    fields = [f for f in card.get("fields") or [] if isinstance(f, dict)]
    if fields:
        parts.append('<div class="fields">')
        for f in fields:
            kind = "inline" if f.get("inline") is True else "block"
            parts.append(
                f'<div class="field {kind}">'
                f'<div class="fname">{_markup(_as_text(f.get("name")), now)}</div>'
                f'<div class="fvalue">{_markup(_as_text(f.get("value")), now)}</div></div>'
            )
        parts.append("</div>")
    thumb = (
        (card.get("thumbnail") or {}).get("url")
        if isinstance(card.get("thumbnail"), dict)
        else None
    )
    if thumb:
        parts.append(f'<div class="eurl">thumbnail: {html.escape(_as_text(thumb))}</div>')
    footer = (
        (card.get("footer") or {}).get("text") if isinstance(card.get("footer"), dict) else None
    )
    stamp = ""
    if card.get("timestamp"):
        try:
            when = datetime.fromisoformat(_as_text(card["timestamp"]))
            stamp = _timestamp(int(when.timestamp()), "f", now)
        except ValueError:
            stamp = ""
    foot = " · ".join(html.escape(x) for x in (_as_text(footer), stamp) if x)
    if foot:
        parts.append(f'<div class="efoot">{foot}</div>')
    parts.append("</div>")
    return "".join(parts)


_CSS = """
:root{color-scheme:dark;--bg:#313338;--panel:#2b2d31;--text:#dbdee1;--muted:#949ba4;--link:#00a8fc}
html,body{background:#313338;background:var(--bg);color:var(--text);margin:0}
body{font:15px/1.4 "gg sans","Noto Sans","Segoe UI",Helvetica,Arial,sans-serif;padding:24px 16px}
main{max-width:760px;margin:0 auto}
h1{font-size:22px;margin:0 0 4px}
h2{font-size:13px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);
margin:32px 0 10px;border-bottom:1px solid #3f4147;padding-bottom:6px}
.banner{background:var(--panel);border-left:4px solid #f0b232;border-radius:4px;
padding:10px 14px;margin:12px 0}
.banner p{margin:2px 0;color:var(--muted)}
.msg{display:flex;gap:12px;margin:10px 0}
.avatar{flex:none;width:40px;height:40px;border-radius:50%;background:#5865f2;color:#fff;
display:flex;align-items:center;justify-content:center;font-weight:700}
.body{min-width:0;flex:1}
.who{font-weight:600;margin-bottom:4px}
.bot{background:#5865f2;color:#fff;font-size:10px;border-radius:3px;padding:1px 4px;
margin-left:6px;vertical-align:middle}
.embed{background:var(--panel);border-left:4px solid #4f545c;border-radius:4px;
padding:8px 16px 12px 12px;margin:0 0 6px;max-width:520px;overflow-wrap:anywhere}
.etitle{font-weight:600;font-size:16px;margin-bottom:4px}
.eurl{color:var(--link);font-size:13px;margin-bottom:4px}
.edesc{font-size:14px}
.fields{display:flex;flex-wrap:wrap;gap:8px 24px;margin-top:8px}
.field.block{flex:0 0 100%}
.field.inline{flex:0 0 calc((100% - 48px) / 3);min-width:0}
.caption{color:var(--muted);font-size:12px;margin:0 0 4px}
.fname{font-weight:600;font-size:13px}
.fvalue{font-size:14px}
.efoot{color:var(--muted);font-size:12px;margin-top:8px}
.ts{background:#4e5058;border-radius:3px;padding:0 2px}
.text{white-space:pre-wrap;overflow-wrap:anywhere}
.empty{color:var(--muted)}
"""


def _message(inner: str, caption: str = "") -> str:
    cap = f'<div class="caption">{html.escape(caption)}</div>' if caption else ""
    return (
        '<div class="msg"><div class="avatar">A</div><div class="body">'
        '<div class="who">Amir Beckett<span class="bot">BOT</span></div>'
        f"{cap}{inner}</div></div>"
    )


def render(
    cards_by_section: Mapping[str, list[dict]],
    text_by_section: Mapping[str, list[str]],
    *,
    now: datetime | None = None,
    note: str | None = None,
    nows_by_section: Mapping[str, list[datetime]] | None = None,
    captions_by_section: Mapping[str, list[str]] | None = None,
) -> str:
    """A self-contained HTML page (inline CSS, no script, no external assets).

    `now` is the default clock for relative times; `nows_by_section` gives individual cards of a
    section their own, and `captions_by_section` labels them. Both are matched by position over
    a section's items: its cards first, then its text lines.
    """
    now = now or datetime.now(UTC)
    names = [*cards_by_section, *(n for n in text_by_section if n not in cards_by_section)]
    sections = []
    for name in names:
        own = (nows_by_section or {}).get(name, [])
        caps = (captions_by_section or {}).get(name, [])
        cards = cards_by_section.get(name, [])
        lines = text_by_section.get(name, [])
        body = ""
        for i, item in enumerate([*cards, *lines]):
            when = own[i] if i < len(own) else now
            caption = caps[i] if i < len(caps) else ""
            if i < len(cards):
                body += _message(_card_html(item, when), caption)
            else:
                body += _message(f'<div class="text">{_markup(item, when)}</div>', caption)
        sections.append(
            f"<section><h2>{html.escape(name)}</h2>{body or '<p class="empty">Nothing.</p>'}"
            "</section>"
        )
    at = _timestamp(int(now.timestamp()), "f", now)
    shown = f"Times shown in America/New_York; relative times are measured from {at}"
    shown += (
        ", except cards posted at their own moment, which read as then." if nows_by_section else "."
    )
    notes = f"<p>{html.escape(note)}</p>" if note else ""
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>Amir Beckett card previews</title>"
        f"<style>{_CSS}</style></head><body><main>"
        "<h1>Amir Beckett card previews</h1>"
        f'<div class="banner"><strong>{html.escape(HEADER)}</strong>{notes}'
        f"<p>{html.escape(shown)}</p></div>"
        f"{''.join(sections)}</main></body></html>"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--from-db", type=Path, default=None, metavar="DB")
    args = parser.parse_args(argv)
    if args.from_db is not None:
        if args.out.resolve() == args.from_db.resolve():
            print(f"refusing to overwrite the database: --out is {args.from_db}", file=sys.stderr)
            return 2
        try:
            cards, text, nows, captions = build_from_db(args.from_db)
        except sqlite3.Error as exc:
            print(f"cannot read previews from {args.from_db}: {exc}", file=sys.stderr)
            return 1
        now = datetime.now(UTC)
        note = (
            f"Rendered from the previews table of {args.from_db.name}: what Amir would have posted."
        )
        page = render(
            cards, text, now=now, note=note, nows_by_section=nows, captions_by_section=captions
        )
    else:
        cards, text, nows = build_from_captures()
        page = render(cards, text, now=PREVIEW_NOW, note=CAPTURE_NOTE, nows_by_section=nows)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(page, encoding="utf-8")
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
