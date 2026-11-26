from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class NewsItem:
    source_id: str
    external_id: str
    title: str
    url: str
    canonical_url: str
    published_at: datetime | None
    excerpt: str
    category: str
    author: str | None


@dataclass
class ParseResult:
    items: list[NewsItem] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


class FeedParseError(Exception):
    """The body was not a usable feed."""
