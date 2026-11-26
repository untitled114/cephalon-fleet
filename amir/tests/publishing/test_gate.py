import logging
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

from amir.publishing.cards import Card, CardField
from amir.publishing.gate import ChannelKey, GatedWriter, SendResult
from amir.storage.repos import MAX_PREVIEWS, PreviewRepo

TS = datetime(2026, 10, 24, 19, 42, tzinfo=UTC)
CARD = Card(
    title="Iceblade of Narin: The Icebind: Hotfix 44.1.3",
    url="https://forums.warframe.com/topic/1",
    timestamp=TS,
    fields=(CardField("Category", "Hotfix"),),
)


class RecordingSender:
    """A ChannelWriter that records every call it receives."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    async def send(
        self,
        channel: ChannelKey,
        *,
        cards: Sequence[Card] = (),
        text: str | None = None,
        role_ping_id: int | None = None,
    ) -> SendResult:
        self.calls.append(
            ("send", (channel,), {"cards": cards, "text": text, "role_ping_id": role_ping_id})
        )
        return SendResult("sent", 42)

    async def edit(
        self, channel: ChannelKey, message_id: int, *, cards: Sequence[Card]
    ) -> SendResult:
        self.calls.append(("edit", (channel, message_id), {"cards": cards}))
        return SendResult("sent", message_id)

    async def find_recent(
        self, channel: ChannelKey, identities: set[tuple[str, str, str]], limit: int = 50
    ) -> int | None:
        self.calls.append(("find_recent", (channel, identities), {"limit": limit}))
        return 42


def test_channel_keys() -> None:
    assert [(k.name, k.value) for k in ChannelKey] == [
        ("NEWS", "news"),
        ("CYCLES", "cycles"),
        ("VOICE_LOG", "voice_log"),
    ]


async def test_disabled_gate_never_reaches_sender(db, clock) -> None:
    sender = RecordingSender()
    previews = PreviewRepo(db, clock)
    gate = GatedWriter(sender, previews, enabled=False)

    assert gate.enabled is False
    sent = await gate.send(ChannelKey.NEWS, cards=[CARD], role_ping_id=7)
    edited = await gate.edit(ChannelKey.CYCLES, 1234, cards=[CARD])
    found = await gate.find_recent(ChannelKey.NEWS, {CARD.identity()})

    assert sender.calls == []
    assert sent == SendResult("previewed", None)
    assert edited == SendResult("previewed", None)
    assert found is None
    assert await previews.count() == 2


async def test_preview_rows_record_payload(db, clock) -> None:
    previews = PreviewRepo(db, clock)
    gate = GatedWriter(RecordingSender(), previews, enabled=False)
    started = clock.now()

    await gate.send(ChannelKey.NEWS, cards=[CARD], text="Ñandú \U0001f409", role_ping_id=7)
    clock.advance(5)
    await gate.edit(ChannelKey.CYCLES, 1234, cards=[CARD])

    newest, oldest = await previews.recent(10)
    assert oldest["channel"] == "news"
    assert oldest["action"] == "send"
    assert oldest["created_at"] == started
    assert oldest["payload"] == {
        "cards": [CARD.to_dict()],
        "text": "Ñandú \U0001f409",
        "role_ping_id": 7,
    }
    assert newest["channel"] == "cycles"
    assert newest["action"] == "edit"
    assert newest["created_at"] == clock.now()
    assert newest["payload"] == {"message_id": 1234, "cards": [CARD.to_dict()]}
    assert newest["id"] > oldest["id"]


async def test_voice_log_previews_not_persisted(db, clock, caplog) -> None:
    caplog.set_level(logging.DEBUG)
    sender = RecordingSender()
    previews = PreviewRepo(db, clock)
    gate = GatedWriter(sender, previews, enabled=False)
    await gate.send(ChannelKey.NEWS, cards=[CARD])
    before = await previews.count()

    # Discord's documentation example user id: distinctive enough that no log line (an object
    # address at DEBUG, say) can contain it by chance.
    member = "80351110224678912"
    sent = await gate.send(ChannelKey.VOICE_LOG, text=f"<@{member}> has arrived in <#456>.")
    edited = await gate.edit(ChannelKey.VOICE_LOG, 99, cards=[CARD])
    as_text = await gate.send("voice_log", text=f"<@{member}> has left.")  # type: ignore[arg-type]

    assert sent == as_text == SendResult("previewed", None)
    assert edited == SendResult("previewed", None)
    assert await previews.count() == before
    assert sender.calls == []
    assert "voice_log" in caplog.text
    assert member not in caplog.text


async def test_enabled_gate_delegates(db, clock) -> None:
    sender = RecordingSender()
    previews = PreviewRepo(db, clock)
    gate = GatedWriter(sender, previews, enabled=True)

    assert gate.enabled is True
    sent = await gate.send(ChannelKey.NEWS, cards=[CARD], text="hi", role_ping_id=7)
    edited = await gate.edit(ChannelKey.CYCLES, 1234, cards=[CARD])
    found = await gate.find_recent(ChannelKey.NEWS, {CARD.identity()}, limit=10)

    assert sent == SendResult("sent", 42)
    assert edited == SendResult("sent", 1234)
    assert found == 42
    assert sender.calls == [
        ("send", (ChannelKey.NEWS,), {"cards": [CARD], "text": "hi", "role_ping_id": 7}),
        ("edit", (ChannelKey.CYCLES, 1234), {"cards": [CARD]}),
        ("find_recent", (ChannelKey.NEWS, {CARD.identity()}), {"limit": 10}),
    ]
    assert await previews.count() == 0


async def test_only_literal_true_enables(db, clock) -> None:
    sender = RecordingSender()
    gate = GatedWriter(sender, PreviewRepo(db, clock), enabled="false")  # type: ignore[arg-type]

    assert gate.enabled is False
    assert await gate.send(ChannelKey.NEWS, cards=[CARD]) == SendResult("previewed", None)
    assert sender.calls == []


async def test_previews_bounded_to_500(db, clock) -> None:
    previews = PreviewRepo(db, clock)
    for n in range(505):
        await previews.add(ChannelKey.NEWS, "send", {"n": n})
        clock.advance(1)

    assert await previews.count() == 500
    rows = await previews.recent(1000)
    assert [row["payload"]["n"] for row in rows] == list(range(504, 4, -1))


async def test_cycle_previews_never_evict_news_previews(db, clock) -> None:
    """While gated every cycle flip is a preview; the news previews the owner must approve
    stay, because the newest rows are kept per channel."""
    previews = PreviewRepo(db, clock)
    await previews.add(ChannelKey.NEWS, "send", {"n": "news"})
    for n in range(MAX_PREVIEWS + 5):
        await previews.add(ChannelKey.CYCLES, "edit", {"n": n})

    rows = await previews.recent(10 * MAX_PREVIEWS)
    assert [row["payload"] for row in rows if row["channel"] == "news"] == [{"n": "news"}]
    cycles = [row["payload"]["n"] for row in rows if row["channel"] == "cycles"]
    assert cycles == list(range(MAX_PREVIEWS + 4, 4, -1))
