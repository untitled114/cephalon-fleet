"""Parsers for the WFCD world-state API and item database (pure, no I/O)."""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from amir.worldstate.official import WorldStateParseError, load_json

_CYCLE_KEYS = {"cetus": "cetusCycle", "vallis": "vallisCycle", "cambion": "cambionCycle"}


@dataclass(frozen=True)
class CycleSnapshot:
    world: str
    state: str
    expiry: datetime


def _iso(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise WorldStateParseError(f"bad timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _doc(data: bytes) -> dict:
    doc = load_json(data)
    if not isinstance(doc, dict):
        raise WorldStateParseError("document is not an object")
    return doc


def parse_cycles(data: bytes) -> dict[str, CycleSnapshot]:
    doc = _doc(data)
    result = {}
    for world, key in _CYCLE_KEYS.items():
        entry = doc.get(key)
        if not isinstance(entry, dict) or not isinstance(entry.get("state"), str):
            raise WorldStateParseError(f"{key} missing or malformed")
        result[world] = CycleSnapshot(world, entry["state"], _iso(entry.get("expiry")))
    return result


def _trader(data: bytes) -> dict | None:
    trader = _doc(data).get("voidTrader")
    return trader if isinstance(trader, dict) else None


def parse_baro_location(data: bytes) -> tuple[datetime, str] | None:
    trader = _trader(data)
    if not trader or not trader.get("activation") or not trader.get("location"):
        return None
    return _iso(trader["activation"]), str(trader["location"])


def parse_inventory_names(data: bytes) -> dict[str, str]:
    trader = _trader(data)
    inventory = trader.get("inventory") if trader else None
    if not isinstance(inventory, list):
        return {}
    return {
        e["uniqueName"]: e["item"]
        for e in inventory
        if isinstance(e, dict) and e.get("uniqueName") and e.get("item")
    }


def parse_item_names(data: bytes) -> dict[str, str]:
    items = load_json(data)
    if not isinstance(items, list):
        raise WorldStateParseError("item database is not a list")
    return {
        e["uniqueName"]: e["name"]
        for e in items
        if isinstance(e, dict) and e.get("uniqueName") and e.get("name")
    }
