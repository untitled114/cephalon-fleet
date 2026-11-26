"""Baro Ki'Teer's three cards: the warning before arrival, the arrival with his stock, and the
warning before departure. Pure: no I/O, no discord imports.

The arrival card lists his mods by name and sums up the rest of his stock as category counts
(the full list would bury them). Every card carries the visit's activation as its timestamp;
Discord timestamps appear only in descriptions. The stock lines are never truncated: they
continue in further embeds of the same message, then in follow-up messages, within Discord's
per-embed and per-message limits.
"""

import re
from collections import Counter
from collections.abc import Mapping
from datetime import datetime

from amir import persona
from amir.baro.names import ItemNameIndex
from amir.publishing.cards import (
    DESCRIPTION,
    EMBEDS_PER_MESSAGE,
    MESSAGE_TOTAL,
    Card,
    message_chars,
)
from amir.publishing.sanitize import escape_markdown, neutralise_mentions, truncate
from amir.worldstate.official import BaroVisit, StockEntry

BARO_COLOR = 0x2E8B8B
FOOTER_LABEL = "Baro Ki'Teer"
# One stock line is capped so it always fits a fresh card, whatever an item name holds.
_LINE_LIMIT = 1024
_HUB_NODE = re.compile(r"([A-Za-z]+)HUB")

MODS = "Mods"
SKINS_AND_ARMOUR = "Skins and armour"
WEAPONS = "Weapons"
SHIP_DECORATIONS = "Ship decorations"
GLYPHS = "Glyphs"
OTHER = "Other"
NO_MODS = "No mods this visit."
_STORE_ROOT = "/Lotus/StoreItems"
_ALSO_ABOARD = "Also aboard:"

# Mod lines, then one summary line of everything else (None when he brought only mods).
ArrivalStock = tuple[list[str], str | None]


def relay_name(node: str, wfcd_location: tuple[datetime, str] | None, activation: datetime) -> str:
    """WFCD's readable location if it describes this visit, else "{Planet} relay" from the node."""
    if wfcd_location is not None:
        wfcd_activation, location = wfcd_location
        if wfcd_activation == activation and location.strip():
            return location
    match = _HUB_NODE.fullmatch(node)
    return f"{match.group(1)} relay" if match else node


def _safe(text: str) -> str:
    return escape_markdown(neutralise_mentions(text))


def _stock_line(name: str, entry: StockEntry) -> str:
    return f"**{_safe(name)}**: {entry.ducats:,} ducats · {entry.credits:,} credits"


def categorise(item_type: str, name: str) -> str:
    """The stock category of one item, from its store path and, for skins, its resolved name."""
    path = item_type.replace(_STORE_ROOT, "", 1)
    if path.startswith(("/Upgrades/Mods/", "/Upgrades/CosmeticEnhancers/Peculiars/")):
        return MODS
    if path.startswith("/Upgrades/Skins/") or name.endswith(" Skin"):
        return SKINS_AND_ARMOUR
    if path.startswith("/Weapons/"):
        return WEAPONS
    if path.startswith("/Types/Items/ShipDecos/"):
        return SHIP_DECORATIONS
    if path.startswith("/Types/StoreItems/AvatarImages/"):
        return GLYPHS
    return OTHER


def arrival_stock(
    visit: BaroVisit, index: ItemNameIndex, wfcd_names: Mapping[str, str]
) -> ArrivalStock:
    """Baro's mods as stock lines, in his order, and one line counting everything else.

    The counts run largest first, ties by category name. The summary is None when he brought
    only mods.
    """
    mods: list[str] = []
    others: Counter[str] = Counter()
    for entry in visit.manifest:
        name = index.resolve(entry.item_type, wfcd_names)
        category = categorise(entry.item_type, name)
        if category == MODS:
            mods.append(_stock_line(name, entry))
        else:
            others[category] += 1
    if not others:
        return mods, None
    counts = sorted(others.items(), key=lambda pair: (-pair[1], pair[0]))
    return mods, f"{_ALSO_ABOARD} " + " · ".join(f"{cat} {count}" for cat, count in counts)


def _times(dt: datetime) -> str:
    unix = int(dt.timestamp())
    return f"<t:{unix}:F> (<t:{unix}:R>)"


def _card(visit: BaroVisit, title: str, description: str) -> Card:
    return Card(
        title=title,
        description=description,
        color=BARO_COLOR,
        timestamp=visit.activation,
        footer=persona.footer(FOOTER_LABEL),
    )


def baro_warning_card(visit: BaroVisit, location: str) -> Card:
    description = (
        f"The Void Trader is due at **{_safe(location)}** on {_times(visit.activation)}.\n\n"
        "A sensible moment to count one's ducats."
    )
    return _card(visit, persona.baro_warning_title(), description)


def baro_departure_card(visit: BaroVisit, location: str) -> Card:
    description = (
        f"He leaves **{_safe(location)}** on {_times(visit.expiry)}.\n\n"
        "Any ducats still burning a hole in your pocket, now would be the time."
    )
    return _card(visit, persona.baro_departure_title(), description)


def baro_arrival_messages(
    visit: BaroVisit, location: str, stock: ArrivalStock | None
) -> list[list[Card]]:
    """The arrival post as messages of cards, from `arrival_stock`'s mod lines and summary.

    `None` (or no stock at all) gives a location-only card. Without mods, the card says so
    above the summary line.
    """
    title = persona.baro_arrival_title(_safe(location))
    open_until = f"He keeps shop until {_times(visit.expiry)}."
    mods, summary = stock or ([], None)
    if not mods and summary is None:
        description = (
            f"{open_until}\n\nHe has yet to lay out his wares. I shall add them here once he does."
        )
        return [[_card(visit, title, description)]]

    def text(lines: list[str]) -> str:
        """One line each; a blank line sets the summary apart, unless it opens a card."""
        joined = ""
        for i, line in enumerate(lines):
            if i:
                joined += "\n\n" if line == summary_line else "\n"
            joined += line
        return joined

    def fits(message: list[Card], card_title: str, text: str) -> bool:
        card = _card(visit, card_title, text)
        return (
            len(message) < EMBEDS_PER_MESSAGE
            and len(text) <= DESCRIPTION
            and message_chars([*message, card]) <= MESSAGE_TOTAL
        )

    lines = [*(mods or [NO_MODS]), *([summary] if summary is not None else [])]
    summary_line = truncate(summary, _LINE_LIMIT) if summary is not None else None
    messages: list[list[Card]] = [[]]
    part = 1
    # Blank lines set the opening, the list and the summary apart.
    prefix = f"{open_until}\n\nHis wares, as listed:\n"
    body: list[str] = []
    for line in (truncate(raw, _LINE_LIMIT) for raw in lines):
        if fits(messages[-1], title, prefix + text([*body, line])):
            body.append(line)
            continue
        messages[-1].append(_card(visit, title, prefix + text(body)))
        part += 1
        title, prefix, body = persona.baro_stock_continued_title(part), "", [line]
        if not fits(messages[-1], title, line):
            messages.append([])
    messages[-1].append(_card(visit, title, prefix + text(body)))
    return messages
