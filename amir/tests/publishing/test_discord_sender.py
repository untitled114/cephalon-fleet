from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import discord
import pytest

from amir.publishing.cards import Card, CardField
from amir.publishing.discord_sender import DiscordSender
from amir.publishing.gate import (
    ChannelKey,
    ChannelUnavailable,
    MessageMissing,
    PermissionDenied,
    PublishError,
    SendResult,
    TransientPublishError,
)

BOT_ID = 1001
NEWS_ID = 2001
CYCLES_ID = 2002
VOICE_LOG_ID = 2003
TS = datetime(2026, 10, 24, 19, 42, tzinfo=UTC)
CARD = Card(
    title="Iceblade of Narin: The Icebind: Hotfix 44.1.3",
    description="Fixes.",
    url="https://forums.warframe.com/topic/1",
    timestamp=TS,
    fields=(CardField("Category", "Hotfix"),),
    footer="Warframe Forums",
)


class FakeResponse:
    """The two attributes discord.py's HTTPException constructor reads."""

    def __init__(self, status: int, reason: str) -> None:
        self.status = status
        self.reason = reason


def http_error(cls: type[discord.HTTPException], status: int) -> discord.HTTPException:
    return cls(FakeResponse(status, "Reason"), "boom")


class FakeMessage:
    def __init__(self, id: int, author_id: int, embeds: list[discord.Embed]) -> None:
        self.id = id
        self.author = SimpleNamespace(id=author_id)
        self.embeds = embeds


class FakePartialMessage:
    def __init__(self, channel: "FakeChannel", message_id: int) -> None:
        self.channel = channel
        self.id = message_id

    async def edit(self, **kwargs: Any) -> None:
        if self.channel.error is not None:
            raise self.channel.error
        self.channel.edits.append((self.id, kwargs))


class FakeChannel(discord.abc.Messageable):
    """A text channel: messageable, with the calls the sender makes recorded."""

    def __init__(self, history: list[FakeMessage] | None = None) -> None:
        self.sends: list[dict[str, Any]] = []
        self.edits: list[tuple[int, dict[str, Any]]] = []
        self.history_limits: list[int | None] = []
        self.error: Exception | None = None
        self._history = history or []

    async def send(self, **kwargs: Any) -> FakeMessage:
        if self.error is not None:
            raise self.error
        self.sends.append(kwargs)
        return FakeMessage(5000 + len(self.sends), BOT_ID, kwargs.get("embeds") or [])

    def get_partial_message(self, message_id: int) -> FakePartialMessage:
        return FakePartialMessage(self, message_id)

    async def history(self, *, limit: int | None = 100) -> AsyncIterator[FakeMessage]:
        self.history_limits.append(limit)
        if self.error is not None:
            raise self.error
        for message in self._history[:limit]:
            yield message


class FakeClient:
    def __init__(
        self,
        cached: dict[int, FakeChannel] | None = None,
        remote: dict[int, FakeChannel] | None = None,
    ) -> None:
        self.cached = cached or {}
        self.remote = remote or {}
        self.fetched: list[int] = []
        self.fetch_error: Exception | None = None
        self.user = SimpleNamespace(id=BOT_ID)

    def get_channel(self, channel_id: int) -> FakeChannel | None:
        return self.cached.get(channel_id)

    async def fetch_channel(self, channel_id: int) -> FakeChannel:
        self.fetched.append(channel_id)
        if self.fetch_error is not None:
            raise self.fetch_error
        if channel_id not in self.remote:
            raise http_error(discord.NotFound, 404)
        return self.remote[channel_id]


IDS = {ChannelKey.NEWS: NEWS_ID, ChannelKey.CYCLES: CYCLES_ID, ChannelKey.VOICE_LOG: VOICE_LOG_ID}


def make_sender(channel: FakeChannel, channel_id: int = NEWS_ID) -> DiscordSender:
    client = FakeClient(cached={channel_id: channel})
    return DiscordSender(client, IDS)  # type: ignore[arg-type]


async def test_send_builds_embeds_from_cards() -> None:
    channel = FakeChannel()
    result = await make_sender(channel).send(ChannelKey.NEWS, cards=[CARD])

    assert result == SendResult("sent", 5001)
    (kwargs,) = channel.sends
    assert kwargs["content"] is None
    (embed,) = kwargs["embeds"]
    assert isinstance(embed, discord.Embed)
    assert embed.to_dict() == discord.Embed.from_dict(CARD.to_dict()).to_dict()
    assert (embed.title, embed.url, embed.timestamp) == (CARD.title, CARD.url, TS)


async def test_sender_uses_no_mentions() -> None:
    channel = FakeChannel()
    sender = make_sender(channel, VOICE_LOG_ID)

    await sender.send(ChannelKey.VOICE_LOG, text="<@123> has arrived in <#456>.")
    await make_sender(channel).send(ChannelKey.NEWS, cards=[CARD])

    for kwargs in channel.sends:
        mentions = kwargs["allowed_mentions"]
        assert isinstance(mentions, discord.AllowedMentions)
        assert mentions.everyone is False
        assert mentions.users is False
        assert mentions.roles is False
        assert mentions.replied_user is False
    # Member and channel mentions still render; AllowedMentions.none() stops the ping.
    assert channel.sends[0]["content"] == "<@123> has arrived in <#456>."


async def test_text_mass_mentions_are_neutralised() -> None:
    channel = FakeChannel()
    await make_sender(channel, VOICE_LOG_ID).send(
        ChannelKey.VOICE_LOG, text="@everyone and @Here, meet <@123> Ñandú \U0001f409"
    )

    content = channel.sends[0]["content"]
    assert "@everyone" not in content
    assert "@Here" not in content
    assert "<@123>" in content
    assert "Ñandú \U0001f409" in content


async def test_text_is_capped_at_message_content_limit() -> None:
    channel = FakeChannel()
    await make_sender(channel, VOICE_LOG_ID).send(ChannelKey.VOICE_LOG, text="x" * 2500)

    content = channel.sends[0]["content"]
    assert len(content) == 2000
    assert content.endswith("…")


async def test_role_ping_only_when_requested() -> None:
    channel = FakeChannel()
    sender = make_sender(channel)

    await sender.send(ChannelKey.NEWS, cards=[CARD], role_ping_id=7)
    await sender.send(ChannelKey.NEWS, cards=[CARD], text="New hotfix.", role_ping_id=7)
    await sender.send(ChannelKey.NEWS, cards=[CARD], role_ping_id=None)

    pinged, pinged_with_text, quiet = channel.sends
    assert pinged["content"] == "<@&7>"
    assert pinged_with_text["content"] == "<@&7> New hotfix."
    for kwargs in (pinged, pinged_with_text):
        mentions = kwargs["allowed_mentions"]
        assert [role.id for role in mentions.roles] == [discord.Object(7).id] == [7]
        assert mentions.everyone is False
        assert mentions.users is False
        assert mentions.replied_user is False
    assert quiet["content"] is None
    assert quiet["allowed_mentions"].roles is False


async def test_edit_uses_partial_message() -> None:
    channel = FakeChannel()
    result = await make_sender(channel, CYCLES_ID).edit(ChannelKey.CYCLES, 777, cards=[CARD])

    assert result == SendResult("sent", 777)
    ((message_id, kwargs),) = channel.edits
    assert message_id == 777
    assert [e.to_dict() for e in kwargs["embeds"]] == [
        discord.Embed.from_dict(CARD.to_dict()).to_dict()
    ]
    assert kwargs["allowed_mentions"].roles is False
    assert channel.sends == []


async def test_channel_resolved_from_cache_then_fetch() -> None:
    cached, remote = FakeChannel(), FakeChannel()
    client = FakeClient(cached={NEWS_ID: cached}, remote={CYCLES_ID: remote})
    sender = DiscordSender(client, IDS)  # type: ignore[arg-type]

    await sender.send(ChannelKey.NEWS, cards=[CARD])
    await sender.send(ChannelKey.CYCLES, cards=[CARD])

    assert len(cached.sends) == 1
    assert len(remote.sends) == 1
    assert client.fetched == [CYCLES_ID]


async def test_error_mapping() -> None:
    channel = FakeChannel()
    sender = make_sender(channel)

    channel.error = http_error(discord.Forbidden, 403)
    with pytest.raises(PermissionDenied) as denied:
        await sender.send(ChannelKey.NEWS, cards=[CARD])
    assert denied.value.transient is False
    assert denied.value.__cause__ is channel.error

    channel.error = http_error(discord.NotFound, 404)
    with pytest.raises(MessageMissing) as missing:
        await sender.edit(ChannelKey.NEWS, 777, cards=[CARD])
    assert missing.value.transient is False

    unmapped = DiscordSender(FakeClient(), {ChannelKey.NEWS: NEWS_ID})  # type: ignore[arg-type]
    with pytest.raises(ChannelUnavailable) as unavailable:
        await unmapped.send(ChannelKey.CYCLES, cards=[CARD])
    assert unavailable.value.transient is False

    channel.error = http_error(discord.HTTPException, 503)
    with pytest.raises(TransientPublishError) as transient:
        await sender.send(ChannelKey.NEWS, cards=[CARD])
    assert transient.value.transient is True


@pytest.mark.parametrize(
    ("error", "expected", "transient"),
    [
        (http_error(discord.HTTPException, 429), TransientPublishError, True),
        (http_error(discord.HTTPException, 500), TransientPublishError, True),
        (http_error(discord.DiscordServerError, 502), TransientPublishError, True),
        (http_error(discord.NotFound, 404), ChannelUnavailable, False),
        (http_error(discord.Forbidden, 403), PermissionDenied, False),
        (http_error(discord.HTTPException, 400), PublishError, False),
    ],
)
async def test_send_error_mapping_table(
    error: Exception, expected: type[PublishError], transient: bool
) -> None:
    channel = FakeChannel()
    channel.error = error

    with pytest.raises(PublishError) as raised:
        await make_sender(channel).send(ChannelKey.NEWS, cards=[CARD])

    assert type(raised.value) is expected
    assert raised.value.transient is transient
    assert raised.value.__cause__ is error


@pytest.mark.parametrize("status", [403, 404])
async def test_unresolvable_channel_is_unavailable(status: int) -> None:
    client = FakeClient()
    client.fetch_error = http_error(
        discord.Forbidden if status == 403 else discord.NotFound, status
    )
    sender = DiscordSender(client, IDS)  # type: ignore[arg-type]

    with pytest.raises(ChannelUnavailable):
        await sender.send(ChannelKey.NEWS, cards=[CARD])
    assert client.fetched == [NEWS_ID]


async def test_server_error_while_resolving_is_transient() -> None:
    client = FakeClient()
    client.fetch_error = http_error(discord.DiscordServerError, 503)
    sender = DiscordSender(client, IDS)  # type: ignore[arg-type]

    with pytest.raises(TransientPublishError):
        await sender.send(ChannelKey.NEWS, cards=[CARD])


async def test_unset_channel_id_is_unavailable_without_api_calls() -> None:
    client = FakeClient()
    sender = DiscordSender(client, {ChannelKey.NEWS: 0})  # type: ignore[arg-type]

    with pytest.raises(ChannelUnavailable):
        await sender.send(ChannelKey.NEWS, cards=[CARD])
    with pytest.raises(ChannelUnavailable):
        await sender.find_recent(ChannelKey.NEWS, {CARD.identity()})
    assert client.fetched == []


def category_channel() -> discord.CategoryChannel:
    """A real discord.py CategoryChannel (no state needed): a guild channel nobody can post in."""
    return discord.CategoryChannel.__new__(discord.CategoryChannel)


@pytest.mark.parametrize("where", ["cached", "fetched"])
async def test_a_channel_that_is_not_messageable_is_unavailable(where: str) -> None:
    category = category_channel()
    assert not isinstance(category, discord.abc.Messageable)
    found = {NEWS_ID: category}
    client = FakeClient(cached=found) if where == "cached" else FakeClient(remote=found)
    sender = DiscordSender(client, IDS)  # type: ignore[arg-type]
    calls = (
        lambda: sender.send(ChannelKey.NEWS, cards=[CARD]),
        lambda: sender.edit(ChannelKey.NEWS, 5001, cards=[CARD]),
        lambda: sender.find_recent(ChannelKey.NEWS, {CARD.identity()}),
    )

    for call in calls:
        with pytest.raises(ChannelUnavailable, match="^channel for news is not a text channel$"):
            await call()


async def test_edit_without_message_id_is_missing() -> None:
    channel = FakeChannel()
    with pytest.raises(MessageMissing):
        await make_sender(channel).edit(ChannelKey.NEWS, 0, cards=[CARD])
    assert channel.edits == []


async def test_too_many_cards_rejected_before_sending() -> None:
    channel = FakeChannel()
    with pytest.raises(PublishError) as raised:
        await make_sender(channel).send(ChannelKey.NEWS, cards=[CARD] * 11)
    assert raised.value.transient is False
    assert channel.sends == []


def embed_for(card: Card) -> discord.Embed:
    return discord.Embed.from_dict(card.to_dict())


async def test_find_recent_matches_identity() -> None:
    other = Card(title="Some other post", url="https://forums.warframe.com/topic/2", timestamp=TS)
    channel = FakeChannel(
        history=[
            FakeMessage(1, author_id=999, embeds=[embed_for(CARD)]),  # not the bot
            FakeMessage(2, author_id=BOT_ID, embeds=[]),
            FakeMessage(3, author_id=BOT_ID, embeds=[embed_for(other)]),
            FakeMessage(4, author_id=BOT_ID, embeds=[embed_for(other), embed_for(CARD)]),
        ]
    )
    sender = make_sender(channel)

    found = await sender.find_recent(ChannelKey.NEWS, {CARD.identity()}, limit=25)

    assert found == 4
    assert channel.history_limits == [25]


async def test_find_recent_ignores_other_authors_and_returns_none() -> None:
    no_timestamp = Card(title="Plains of Eidolon")
    channel = FakeChannel(
        history=[
            FakeMessage(1, author_id=999, embeds=[embed_for(CARD)]),
            FakeMessage(2, author_id=BOT_ID, embeds=[embed_for(no_timestamp)]),
        ]
    )
    sender = make_sender(channel)

    assert await sender.find_recent(ChannelKey.NEWS, {CARD.identity()}) is None
    assert channel.history_limits == [50]
    assert await sender.find_recent(ChannelKey.NEWS, {no_timestamp.identity()}) == 2


async def test_find_recent_matches_long_title_as_discord_stores_it() -> None:
    long_card = Card(title=CARD.title * 6, url=CARD.url, timestamp=TS)
    channel = FakeChannel(history=[FakeMessage(7, author_id=BOT_ID, embeds=[embed_for(long_card)])])

    assert await make_sender(channel).find_recent(ChannelKey.NEWS, {long_card.identity()}) == 7


async def test_find_recent_maps_errors() -> None:
    channel = FakeChannel()
    channel.error = http_error(discord.Forbidden, 403)

    with pytest.raises(PermissionDenied):
        await make_sender(channel).find_recent(ChannelKey.NEWS, {CARD.identity()})
