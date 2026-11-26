from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta

import pytest

from amir import persona
from amir.baro.cards import (
    MODS,
    NO_MODS,
    arrival_stock,
    baro_arrival_messages,
    baro_departure_card,
    baro_warning_card,
    categorise,
    relay_name,
)
from amir.baro.names import ItemNameIndex
from amir.publishing.cards import (
    DESCRIPTION,
    EMBEDS_PER_MESSAGE,
    MESSAGE_TOTAL,
    Card,
    message_chars,
)
from amir.worldstate.official import parse_void_trader
from amir.worldstate.wfcd import parse_baro_location, parse_inventory_names, parse_item_names

from .conftest import copies, stock_lines

# Pinned from the real 2026-10-24 capture.
REAL_MODS = [
    "Peculiar End",
    "Primed Fast Hands",
    "Primed Point Blank",
    "Primed Charged Shell",
    "Volcanic Edge",
    "Primed Cleanse Grineer",
    "Mark Of The Beast",
    "Primed Expel Orokin",
    "Primed Fulmination",
    "Primed Expel The Murmur",
    "Primed Cleanse The Murmur",
]
REAL_COUNTS = "Skins and armour 28 · Ship decorations 7 · Other 4 · Glyphs 3 · Weapons 3"


@pytest.fixture
def real(captured):
    visit = parse_void_trader(captured("official_worldstate_baro_present.json"))
    wfcd = captured("wfcd_pc_baro_present.json")
    index = ItemNameIndex(parse_item_names(captured("wfcd_items_names.json")))
    return visit, parse_baro_location(wfcd), parse_inventory_names(wfcd), index


def _only(visit, index, names, *, mods: bool):
    """The real visit with only its mods, or with every mod removed."""
    kept = tuple(
        e
        for e in visit.manifest
        if (categorise(e.item_type, index.resolve(e.item_type, names)) == MODS) is mods
    )
    return replace(visit, manifest=kept)


def _unix(dt: datetime) -> int:
    return int(dt.timestamp())


def _stock_in(cards: list[Card], lines: list[str]) -> list[str]:
    """The stock lines found in the cards' descriptions, in order."""
    wanted = set(lines)
    return [line for card in cards for line in card.description.split("\n") if line in wanted]


def test_relay_name_prefers_wfcd_location_for_the_same_visit(real):
    visit, location, _, _ = real
    assert relay_name(visit.node, location, visit.activation) == "Strata Relay (Earth)"


def test_relay_fallback(real):
    visit, (activation, name), _, _ = real
    other_visit = (activation + timedelta(days=14), name)
    assert relay_name(visit.node, other_visit, visit.activation) == "Earth relay"
    assert relay_name(visit.node, None, visit.activation) == "Earth relay"


def test_relay_name_keeps_an_unrecognised_node(real):
    visit, _, _, _ = real
    node = visit.node.removesuffix("HUB")
    assert relay_name(node, None, visit.activation) == node


def test_categorise_real_manifest(real):
    visit, _, names, index = real
    counts = Counter(
        categorise(e.item_type, index.resolve(e.item_type, names)) for e in visit.manifest
    )
    assert counts == {
        "Mods": 11,
        "Skins and armour": 28,
        "Ship decorations": 7,
        "Other": 4,
        "Glyphs": 3,
        "Weapons": 3,
    }
    assert sum(counts.values()) == len(visit.manifest) == 56


def _by_name(visit, index, names) -> dict[str, str]:
    """Each real stock item's path, keyed by its resolved name."""
    return {index.resolve(e.item_type, names): e.item_type for e in visit.manifest}


def test_categorise_edge_items_from_the_capture(real):
    visit, _, names, index = real
    tannukai, peculiar = "Tannukai Longsword Skin", "Peculiar End"
    paths = _by_name(visit, index, names)
    assert paths[tannukai].removeprefix("/Lotus/StoreItems").startswith("/Weapons/")
    assert categorise(paths[tannukai], tannukai) == "Skins and armour"
    assert "/Upgrades/CosmeticEnhancers/Peculiars/" in paths[peculiar]
    assert categorise(paths[peculiar], peculiar) == "Mods"


def test_categorise_rules_apply_in_order(real):
    visit, _, names, index = real
    paths = _by_name(visit, index, names)
    mod, weapon = "Primed Fast Hands", "Quanta Vandal"
    skin = next(name for name in paths if name.endswith(" Skin"))
    # A mod path wins over a skin name; a skin name wins over a weapon path.
    assert categorise(paths[mod], skin) == "Mods"
    assert categorise(paths[weapon], skin) == "Skins and armour"
    assert categorise(paths[weapon], weapon) == "Weapons"
    # Only the store prefix is ignored: the same path outside /Lotus/StoreItems is Other.
    moved = paths[mod].replace("/Lotus/StoreItems", "/Lotus", 1)
    assert categorise(moved, mod) == "Other"


def test_arrival_stock_lists_real_mods_in_order(real):
    visit, _, names, index = real
    mods, summary = arrival_stock(visit, index, names)
    assert len(mods) == 11
    paths = _by_name(visit, index, names)
    prices = {e.item_type: e for e in visit.manifest}
    for name, line in zip(REAL_MODS, mods, strict=True):
        entry = prices[paths[name]]
        expected = f"**{name}**: {entry.ducats:,} ducats · {entry.credits:,} credits"
        assert line == expected
    assert any("1,000,001 credits" in line for line in mods)
    assert summary == f"Also aboard: {REAL_COUNTS}"
    for gone in ("Eskhatos Pauldrons", "Zephyr Immortal Skin", "Eskhatos Ki'Teer Floof"):
        assert not any(gone in line for line in mods)


def test_arrival_stock_without_mods(real):
    visit, _, names, index = real
    no_mods = _only(visit, index, names, mods=False)
    assert len(no_mods.manifest) == 45
    assert arrival_stock(no_mods, index, names) == ([], f"Also aboard: {REAL_COUNTS}")


def test_arrival_stock_with_only_mods(real):
    visit, _, names, index = real
    only_mods = _only(visit, index, names, mods=True)
    mods, summary = arrival_stock(only_mods, index, names)
    assert mods == arrival_stock(visit, index, names)[0]
    assert summary is None


def test_summary_sorts_by_count_then_name(real):
    visit, _, names, index = real

    def of(category: str) -> list:
        return [
            e
            for e in visit.manifest
            if categorise(e.item_type, index.resolve(e.item_type, names)) == category
        ]

    # A fourth weapon ties Other and moves ahead of Glyphs; at equal counts, names decide.
    more = replace(visit, manifest=(*visit.manifest, of("Weapons")[0]))
    assert arrival_stock(more, index, names)[1] == (
        "Also aboard: Skins and armour 28 · Ship decorations 7 · Other 4 · Weapons 4 · Glyphs 3"
    )
    fewer = replace(visit, manifest=tuple(e for e in visit.manifest if e is not of("Glyphs")[0]))
    assert arrival_stock(fewer, index, names)[1] == (
        "Also aboard: Skins and armour 28 · Ship decorations 7 · Other 4 · Weapons 3 · Glyphs 2"
    )


def test_mod_names_cannot_ping_or_break_markdown(real, captured):
    visit, _, names, index = real
    db_names = parse_item_names(captured("wfcd_items_names.json"))
    paths = _by_name(visit, index, names)
    key = paths[REAL_MODS[1]].replace("/StoreItems", "", 1)
    assert db_names[key] == REAL_MODS[1]
    hostile = ItemNameIndex({**db_names, key: f"@everyone <@&1> **{db_names[key]}**"})
    plain, _ = arrival_stock(visit, index, names)
    mods, _ = arrival_stock(visit, hostile, names)
    (line,) = [h for h, p in zip(mods, plain, strict=True) if h != p]
    assert line == mods[1]
    assert "@everyone" not in line
    assert "<@&" not in line
    assert f"\\*\\*{db_names[key]}\\*\\*" in line


def test_arrival_with_real_stock_fits_one_embed(real):
    visit, location, names, index = real
    where = relay_name(visit.node, location, visit.activation)
    stock = arrival_stock(visit, index, names)
    messages = baro_arrival_messages(visit, where, stock)
    assert len(messages) == 1
    assert len(messages[0]) == 1
    card = messages[0][0]
    assert card.title == persona.baro_arrival_title(where)
    assert len(card.description) <= DESCRIPTION
    assert message_chars(messages[0]) <= MESSAGE_TOTAL
    lines = stock_lines(visit, index, names)
    assert len(lines) == 12
    assert _stock_in([card], lines) == lines
    assert card.description.splitlines()[-13:] == [*lines[:-1], "", lines[-1]]
    for gone in ("Eskhatos Pauldrons", "Zephyr Immortal Skin", NO_MODS):
        assert gone not in card.description
    expiry = _unix(visit.expiry)
    assert f"<t:{expiry}:F>" in card.description
    assert f"<t:{expiry}:R>" in card.description
    assert card.timestamp == visit.activation


def test_arrival_without_mods_says_so_and_keeps_the_summary(real):
    visit, location, names, index = real
    where = relay_name(visit.node, location, visit.activation)
    no_mods = _only(visit, index, names, mods=False)
    ((card,),) = baro_arrival_messages(no_mods, where, arrival_stock(no_mods, index, names))
    assert NO_MODS == "No mods this visit."
    assert card.description.splitlines()[-3:] == [NO_MODS, "", f"Also aboard: {REAL_COUNTS}"]
    assert " ducats · " not in card.description


def test_arrival_with_only_mods_omits_the_summary(real):
    visit, location, names, index = real
    where = relay_name(visit.node, location, visit.activation)
    only_mods = _only(visit, index, names, mods=True)
    stock = arrival_stock(only_mods, index, names)
    ((card,),) = baro_arrival_messages(only_mods, where, stock)
    assert card.description.splitlines()[-11:] == stock[0]
    assert "Also aboard" not in card.description
    assert NO_MODS not in card.description


def test_location_only_arrival_card(real):
    visit, location, names, index = real
    where = relay_name(visit.node, location, visit.activation)
    messages = baro_arrival_messages(visit, where, None)
    assert baro_arrival_messages(visit, where, ([], None)) == messages
    assert len(messages) == 1
    assert len(messages[0]) == 1
    card = messages[0][0]
    assert card.title == persona.baro_arrival_title(where)
    assert _stock_in([card], stock_lines(visit, index, names)) == []
    assert NO_MODS not in card.description
    expiry = _unix(visit.expiry)
    assert f"<t:{expiry}:F>" in card.description
    assert f"<t:{expiry}:R>" in card.description
    assert card.timestamp == visit.activation


def test_overflow_splits_messages(real):
    visit, location, names, index = real
    where = relay_name(visit.node, location, visit.activation)
    mods, summary = arrival_stock(visit, index, names)
    many = mods * copies(mods)
    lines = [*many, summary]
    messages = baro_arrival_messages(visit, where, (many, summary))
    assert len(messages) > 2
    for message in messages:
        assert 1 <= len(message) <= EMBEDS_PER_MESSAGE
        assert message_chars(message) <= MESSAGE_TOTAL
        for card in message:
            assert len(card.description) <= DESCRIPTION
            assert card.timestamp == visit.activation
    cards = [card for message in messages for card in message]
    assert _stock_in(cards, lines) == lines
    assert cards[0].title == persona.baro_arrival_title(where)
    # Distinct titles keep each message's identity distinct for outbox reconciliation.
    titles = [card.title for card in cards]
    assert len(set(titles)) == len(titles)


def test_warning_and_departure_cards(real):
    visit, location, _, _ = real
    where = relay_name(visit.node, location, visit.activation)
    warning = baro_warning_card(visit, where)
    departure = baro_departure_card(visit, where)
    arrival, expiry = _unix(visit.activation), _unix(visit.expiry)
    assert warning.title == persona.baro_warning_title()
    assert f"<t:{arrival}:F>" in warning.description
    assert f"<t:{arrival}:R>" in warning.description
    assert departure.title == persona.baro_departure_title()
    assert f"<t:{expiry}:F>" in departure.description
    assert f"<t:{expiry}:R>" in departure.description
    for card in (warning, departure):
        assert where in card.description
        assert card.timestamp == visit.activation


def test_baro_cards_set_their_parts_apart_with_blank_lines(real):
    # Each part of the card stands apart, so the text never reads as one cluster.
    visit, location, names, index = real
    where = relay_name(visit.node, location, visit.activation)
    arrival, expiry = _unix(visit.activation), _unix(visit.expiry)
    assert baro_warning_card(visit, where).description == (
        f"The Void Trader is due at **{where}** on <t:{arrival}:F> (<t:{arrival}:R>).\n\n"
        "A sensible moment to count one's ducats."
    )
    assert baro_departure_card(visit, where).description == (
        f"He leaves **{where}** on <t:{expiry}:F> (<t:{expiry}:R>).\n\n"
        "Any ducats still burning a hole in your pocket, now would be the time."
    )
    mods, summary = arrival_stock(visit, index, names)
    ((card,),) = baro_arrival_messages(visit, where, (mods, summary))
    assert card.description == (
        f"He keeps shop until <t:{expiry}:F> (<t:{expiry}:R>).\n\nHis wares, as listed:\n"
        + "\n".join(mods)
        + f"\n\n{summary}"
    )
    ((card,),) = baro_arrival_messages(visit, where, ([], summary))
    assert card.description.endswith(f"His wares, as listed:\n{NO_MODS}\n\n{summary}")


def test_a_continued_card_never_opens_with_a_blank_line(real):
    visit, location, names, index = real
    where = relay_name(visit.node, location, visit.activation)
    mods, summary = arrival_stock(visit, index, names)
    pool = mods * copies(mods)
    summary_opened_a_card = False
    for count in range(1, len(pool) + 1):
        messages = baro_arrival_messages(visit, where, (pool[:count], summary))
        for card in (card for message in messages for card in message):
            assert not card.description.startswith("\n"), count
            assert "\n\n\n" not in card.description, count
            if card.description.startswith(summary):
                summary_opened_a_card = True
    assert summary_opened_a_card  # the edge was really exercised


def test_discord_timestamps_only_in_descriptions(real):
    visit, location, names, index = real
    where = relay_name(visit.node, location, visit.activation)
    mods, summary = arrival_stock(visit, index, names)
    overflow = baro_arrival_messages(visit, where, (mods * copies(mods), summary))
    cards = [
        baro_warning_card(visit, where),
        baro_departure_card(visit, where),
        *baro_arrival_messages(visit, where, None)[0],
        *(card for message in overflow for card in message),
    ]
    for card in cards:
        assert "<t:" not in card.title
        assert "<t:" not in (card.footer or "")


def test_location_cannot_ping(real):
    visit, (activation, name), _, _ = real
    where = relay_name(visit.node, (activation, f"@everyone <@&1> {name}"), visit.activation)
    cards = [
        baro_warning_card(visit, where),
        baro_departure_card(visit, where),
        *baro_arrival_messages(visit, where, None)[0],
    ]
    for card in cards:
        assert "@everyone" not in card.title + card.description
        assert "<@&" not in card.title + card.description
        assert name in card.title + card.description
