import json
from datetime import UTC, datetime

import pytest

from amir.worldstate.official import WorldStateParseError
from amir.worldstate.wfcd import (
    CycleSnapshot,
    parse_baro_location,
    parse_cycles,
    parse_inventory_names,
    parse_item_names,
)

CAPTURE = "wfcd_pc_baro_present.json"


def test_cycles_from_capture(captured):
    cycles = parse_cycles(captured(CAPTURE))
    assert set(cycles) == {"cetus", "vallis", "cambion"}
    assert cycles["cetus"] == CycleSnapshot(
        "cetus", "night", datetime(2026, 10, 25, 1, 42, tzinfo=UTC)
    )
    assert cycles["vallis"].state == "cold"
    assert cycles["vallis"].expiry == datetime(2026, 10, 25, 1, 33, 28, tzinfo=UTC)
    assert cycles["cambion"].state == "vome"
    assert cycles["cambion"].expiry == datetime(2026, 10, 25, 1, 42, tzinfo=UTC)


def test_cycles_errors(captured):
    doc = json.loads(captured(CAPTURE))
    with pytest.raises(WorldStateParseError):
        parse_cycles(b"<html>")
    missing = json.loads(json.dumps(doc))
    del missing["vallisCycle"]
    with pytest.raises(WorldStateParseError):
        parse_cycles(json.dumps(missing).encode())
    nostate = json.loads(json.dumps(doc))
    del nostate["cetusCycle"]["state"]
    with pytest.raises(WorldStateParseError):
        parse_cycles(json.dumps(nostate).encode())
    bad = json.loads(json.dumps(doc))
    bad["cambionCycle"]["expiry"] = "soon"
    with pytest.raises(WorldStateParseError):
        parse_cycles(json.dumps(bad).encode())


def test_location_and_names(captured):
    data = captured(CAPTURE)
    assert parse_baro_location(data) == (
        datetime(2026, 10, 23, 13, 0, tzinfo=UTC),
        "Strata Relay (Earth)",
    )
    names = parse_inventory_names(data)
    assert len(names) == 56
    assert (
        names["/Lotus/StoreItems/Upgrades/Skins/Armor/EvilBaroArmour/EvilBaroArmourA"]
        == "Evil Baro Armour A"
    )
    assert len(parse_item_names(captured("wfcd_items_names.json"))) == 17_934


def test_baro_away_and_missing(captured):
    doc = json.loads(captured(CAPTURE))
    del doc["voidTrader"]["inventory"]
    assert parse_inventory_names(json.dumps(doc).encode()) == {}
    assert parse_baro_location(json.dumps(doc).encode()) is not None
    del doc["voidTrader"]["location"]
    assert parse_baro_location(json.dumps(doc).encode()) is None
    del doc["voidTrader"]
    assert parse_baro_location(json.dumps(doc).encode()) is None
    assert parse_inventory_names(json.dumps(doc).encode()) == {}


def test_item_names_skips_incomplete_and_rejects_non_list(captured):
    raw = captured("wfcd_items_names.json")
    baseline = parse_item_names(raw)
    items = json.loads(raw)
    no_name = {k: v for k, v in items[0].items() if k != "name"}
    no_unique = {k: v for k, v in items[1].items() if k != "uniqueName"}
    polluted = [*items, no_name, no_unique, items[2]["name"]]
    assert parse_item_names(json.dumps(polluted).encode()) == baseline
    assert len(baseline) == 17_934
    with pytest.raises(WorldStateParseError):
        parse_item_names(captured("wfcd_pc_baro_present.json"))
    with pytest.raises(WorldStateParseError):
        parse_item_names(b"<html>")


def test_non_object_top_level_raises(captured):
    real_list = captured("wfcd_items_names.json")
    for parser in (parse_cycles, parse_baro_location, parse_inventory_names):
        with pytest.raises(WorldStateParseError):
            parser(real_list)
