"""Helpers shared by the Baro card and service tests (plain functions, imported from here)."""

from amir.baro.cards import arrival_stock
from amir.publishing.cards import MESSAGE_TOTAL


def stock_lines(visit, index, names) -> list[str]:
    """Every stock line the arrival card should show, in order: mods, then the summary."""
    mods, summary = arrival_stock(visit, index, names)
    return [*mods, *([summary] if summary is not None else [])]


def copies(lines: list[str]) -> int:
    """Enough copies of `lines` to need more than two messages."""
    return 2 * MESSAGE_TOTAL // len("\n".join(lines)) + 1
