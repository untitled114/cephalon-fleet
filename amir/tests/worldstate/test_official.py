import json
from datetime import UTC, datetime

import pytest

from amir.worldstate.official import BaroVisit, StockEntry, WorldStateParseError, parse_void_trader

CAPTURE = "official_worldstate_baro_present.json"
ARMOUR = "/Lotus/StoreItems/Upgrades/Skins/Armor/EvilBaroArmour/EvilBaroArmourA"


def test_baro_present(captured):
    visit = parse_void_trader(captured(CAPTURE))
    assert isinstance(visit, BaroVisit)
    assert visit.activation == datetime(2026, 10, 23, 13, 0, tzinfo=UTC)
    assert visit.expiry == datetime(2026, 10, 25, 13, 0, tzinfo=UTC)
    assert visit.node == "EarthHUB"
    assert len(visit.manifest) == 56
    assert visit.manifest[0] == StockEntry(ARMOUR, 303, 303303)


def _shift(doc: dict, days: int) -> None:
    trader = doc["VoidTraders"][0]
    for key in ("Activation", "Expiry"):
        ms = int(trader[key]["$date"]["$numberLong"]) + days * 86_400_000
        trader[key]["$date"]["$numberLong"] = str(ms)


def test_baro_absent_derived(captured):
    doc = json.loads(captured(CAPTURE))
    del doc["VoidTraders"][0]["Manifest"]
    _shift(doc, 14)
    visit = parse_void_trader(json.dumps(doc).encode())
    assert visit.manifest == ()
    assert visit.activation == datetime(2026, 11, 6, 13, 0, tzinfo=UTC)


def test_numberlong_accepts_int(captured):
    doc = json.loads(captured(CAPTURE))
    trader = doc["VoidTraders"][0]
    trader["Activation"]["$date"]["$numberLong"] = int(trader["Activation"]["$date"]["$numberLong"])
    visit = parse_void_trader(json.dumps(doc).encode())
    assert visit.activation == datetime(2026, 10, 23, 13, 0, tzinfo=UTC)


def test_empty_list_returns_none(captured):
    doc = json.loads(captured(CAPTURE))
    doc["VoidTraders"] = []
    assert parse_void_trader(json.dumps(doc).encode()) is None


def test_missing_voidtraders_raises(captured):
    doc = json.loads(captured(CAPTURE))
    del doc["VoidTraders"]
    with pytest.raises(WorldStateParseError):
        parse_void_trader(json.dumps(doc).encode())
    with pytest.raises(WorldStateParseError):
        parse_void_trader(b"<html>")


def test_malformed_trader_raises(captured):
    doc = json.loads(captured(CAPTURE))
    del doc["VoidTraders"][0]["Expiry"]
    with pytest.raises(WorldStateParseError):
        parse_void_trader(json.dumps(doc).encode())
    with pytest.raises(WorldStateParseError):
        parse_void_trader(captured("wfcd_items_names.json"))  # a real JSON list, not an object
