from datetime import UTC, datetime

import pytest

from amir.publishing.cards import (
    CONTENT,
    DESCRIPTION,
    EMBEDS_PER_MESSAGE,
    FIELD_NAME,
    FIELD_VALUE,
    FIELDS,
    FOOTER,
    MESSAGE_TOTAL,
    TITLE,
    Card,
    CardField,
    message_chars,
)


def test_constants() -> None:
    assert (TITLE, DESCRIPTION, FIELDS, FIELD_NAME, FIELD_VALUE) == (256, 4096, 25, 256, 1024)
    assert (FOOTER, MESSAGE_TOTAL, CONTENT, EMBEDS_PER_MESSAGE) == (2048, 6000, 2000, 10)


def test_description_truncated_to_limit() -> None:
    d = Card(title="t", description="x" * 5000).to_dict()
    assert len(d["description"]) == 4096
    assert d["description"].endswith("…")


def test_title_field_footer_limits() -> None:
    card = Card(
        title="t" * 300,
        fields=(CardField("n" * 300, "v" * 2000),),
        footer="f" * 3000,
    )
    d = card.to_dict()
    assert len(d["title"]) == TITLE
    assert len(d["fields"][0]["name"]) == FIELD_NAME
    assert len(d["fields"][0]["value"]) == FIELD_VALUE
    assert len(d["footer"]["text"]) == FOOTER


def test_fields_capped_at_25() -> None:
    card = Card(title="t", fields=tuple(CardField(f"n{i}", "v") for i in range(40)))
    assert len(card.to_dict()["fields"]) == 25


def test_empty_keys_omitted_and_shape() -> None:
    assert Card(title="t").to_dict() == {"title": "t", "color": 0x8A6D3B}
    ts = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
    d = Card(
        title="t",
        description="d",
        url="https://x",
        timestamp=ts,
        fields=(CardField("a", "b", True),),
        footer="f",
        thumbnail_url="https://img",
    ).to_dict()
    assert d == {
        "title": "t",
        "description": "d",
        "url": "https://x",
        "color": 0x8A6D3B,
        "timestamp": ts.isoformat(),
        "fields": [{"name": "a", "value": "b", "inline": True}],
        "footer": {"text": "f"},
        "thumbnail": {"url": "https://img"},
    }


def test_round_trip_from_dict() -> None:
    card = Card(
        title="t",
        description="d",
        url="https://x",
        color=123,
        timestamp=datetime(2026, 10, 24, 12, 0, tzinfo=UTC),
        fields=(CardField("a", "b", True), CardField("c", "d")),
        footer="f",
        thumbnail_url="https://img",
    )
    assert Card.from_dict(card.to_dict()) == card
    assert Card.from_dict(Card(title="t").to_dict()) == Card(title="t")


def test_identity() -> None:
    ts = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
    assert Card(title="t").identity() == ("t", "", "")
    assert Card(title="t", url="u", timestamp=ts).identity() == ("t", "u", ts.isoformat())


def test_identity_matches_emitted_values() -> None:
    ts = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
    card = Card(title="t" * 300, url="https://x", timestamp=ts)
    d = card.to_dict()
    assert card.identity() == (d["title"], d["url"], d["timestamp"])
    assert len(card.identity()[0]) == TITLE
    assert Card.from_dict(d).identity() == card.identity()


def test_message_chars_counts_embed_text() -> None:
    a = Card(title="abc", description="de", fields=(CardField("n", "vv"),), footer="fff")
    b = Card(title="x")
    assert message_chars([a, b]) == (3 + 2 + 1 + 2 + 3) + 1


def test_timestamp_normalised_to_utc_whole_seconds() -> None:
    from datetime import timedelta, timezone

    est = timezone(timedelta(hours=-5))
    card = Card(title="t", timestamp=datetime(2026, 10, 24, 7, 0, 0, 654321, tzinfo=est))
    assert card.timestamp == datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
    assert card.timestamp is not None and card.timestamp.microsecond == 0
    assert card.timestamp.utcoffset() == timedelta(0)
    assert card.to_dict()["timestamp"] == "2026-10-24T12:00:00+00:00"


def test_naive_timestamp_rejected() -> None:
    with pytest.raises(ValueError):
        Card(title="t", timestamp=datetime(2026, 10, 24, 12, 0))
