"""Parser for the official Warframe world-state (pure, no I/O)."""

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


class WorldStateParseError(Exception):
    """The body is not a usable world-state document."""


@dataclass(frozen=True)
class StockEntry:
    item_type: str
    ducats: int
    credits: int


@dataclass(frozen=True)
class BaroVisit:
    activation: datetime
    expiry: datetime
    node: str
    manifest: tuple[StockEntry, ...]


def load_json(data: bytes) -> Any:
    try:
        return json.loads(data)
    except (ValueError, UnicodeDecodeError) as exc:
        raise WorldStateParseError("body is not JSON") from exc


def _date(value: Any) -> datetime:
    try:
        ms = int(value["$date"]["$numberLong"])
        return datetime.fromtimestamp(ms / 1000, tz=UTC)
    except (KeyError, TypeError, ValueError, OverflowError, OSError) as exc:
        raise WorldStateParseError(f"bad $date value: {value!r}") from exc


def parse_void_trader(data: bytes) -> BaroVisit | None:
    doc = load_json(data)
    if not isinstance(doc, dict) or not isinstance(doc.get("VoidTraders"), list):
        raise WorldStateParseError("VoidTraders missing")
    if not doc["VoidTraders"]:
        return None
    trader = doc["VoidTraders"][0]
    try:
        node = trader["Node"]
        manifest = tuple(
            StockEntry(str(e["ItemType"]), int(e["PrimePrice"]), int(e["RegularPrice"]))
            for e in trader.get("Manifest", [])
        )
        activation = _date(trader["Activation"])
        expiry = _date(trader["Expiry"])
    except (KeyError, TypeError, ValueError) as exc:
        raise WorldStateParseError("malformed VoidTraders[0]") from exc
    return BaroVisit(activation, expiry, str(node), manifest)
