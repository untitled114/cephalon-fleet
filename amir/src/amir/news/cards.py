"""News items rendered as Amir's cards."""

from amir.news.model import NewsItem
from amir.persona import footer
from amir.publishing.cards import Card, CardField
from amir.publishing.sanitize import neutralise_mentions

HOTFIX_COLOR = 0xB5483A
UPDATE_COLOR = 0x3F6E8C
ANNOUNCEMENT_COLOR = 0xB08D3C

_COLORS = {
    "Hotfix": HOTFIX_COLOR,
    "Update": UPDATE_COLOR,
    "Announcement": ANNOUNCEMENT_COLOR,
}


def news_card(item: NewsItem, *, source_label: str) -> Card:
    color = _COLORS.get(item.category)
    extra = {} if color is None else {"color": color}
    return Card(
        title=neutralise_mentions(item.title),
        description=neutralise_mentions(item.excerpt),
        url=item.canonical_url,
        timestamp=item.published_at,
        fields=(
            CardField("Category", item.category),
            CardField("Source", source_label),
        ),
        footer=footer(source_label),
        **extra,
    )
