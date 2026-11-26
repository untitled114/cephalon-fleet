"""Embed-shaped cards with Discord limits enforced. No discord.py imports."""

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from amir.publishing.sanitize import truncate

TITLE = 256
DESCRIPTION = 4096
FIELDS = 25
FIELD_NAME = 256
FIELD_VALUE = 1024
FOOTER = 2048
MESSAGE_TOTAL = 6000
CONTENT = 2000
EMBEDS_PER_MESSAGE = 10


@dataclass(frozen=True)
class CardField:
    name: str
    value: str
    inline: bool = False


@dataclass(frozen=True)
class Card:
    title: str
    description: str = ""
    url: str | None = None
    color: int = 0x8A6D3B
    timestamp: datetime | None = None
    fields: tuple[CardField, ...] = ()
    footer: str | None = None
    thumbnail_url: str | None = None

    def __post_init__(self) -> None:
        if self.timestamp is not None:
            if self.timestamp.tzinfo is None:
                raise ValueError("Card timestamp must be timezone-aware")
            normalised = self.timestamp.astimezone(UTC).replace(microsecond=0)
            object.__setattr__(self, "timestamp", normalised)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"title": truncate(self.title, TITLE)}
        if self.description:
            out["description"] = truncate(self.description, DESCRIPTION)
        if self.url:
            out["url"] = self.url
        out["color"] = self.color
        if self.timestamp:
            out["timestamp"] = self.timestamp.isoformat()
        if self.fields:
            out["fields"] = [
                {
                    "name": truncate(f.name, FIELD_NAME),
                    "value": truncate(f.value, FIELD_VALUE),
                    "inline": f.inline,
                }
                for f in self.fields[:FIELDS]
            ]
        if self.footer:
            out["footer"] = {"text": truncate(self.footer, FOOTER)}
        if self.thumbnail_url:
            out["thumbnail"] = {"url": self.thumbnail_url}
        return out

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Card":
        ts = d.get("timestamp")
        return cls(
            title=d.get("title", ""),
            description=d.get("description", ""),
            url=d.get("url"),
            color=d.get("color", 0x8A6D3B),
            timestamp=datetime.fromisoformat(ts) if ts else None,
            fields=tuple(
                CardField(f["name"], f["value"], f.get("inline", False))
                for f in d.get("fields", [])
            ),
            footer=(d.get("footer") or {}).get("text"),
            thumbnail_url=(d.get("thumbnail") or {}).get("url"),
        )

    def identity(self) -> tuple[str, str, str]:
        """(title, url, timestamp) exactly as `to_dict()` emits them, as Discord stores them."""
        d = self.to_dict()
        return (d["title"], d.get("url", ""), d.get("timestamp", ""))


def message_chars(cards: Sequence[Card]) -> int:
    total = 0
    for card in cards:
        d = card.to_dict()
        total += len(d["title"]) + len(d.get("description", ""))
        total += len(d.get("footer", {}).get("text", ""))
        for f in d.get("fields", []):
            total += len(f["name"]) + len(f["value"])
    return total
