"""DiscordSender: the only module that talks to Discord channels (discord.py adapter).

It is only ever reached through an enabled GatedWriter. Every send uses
AllowedMentions.none(), except the optional configured role ping, which allows exactly that
one role. discord.py errors are translated into the PublishError family.
"""

import re
from collections.abc import Mapping, Sequence
from typing import Any

import discord

from amir.publishing.cards import CONTENT, EMBEDS_PER_MESSAGE, Card
from amir.publishing.gate import (
    ChannelKey,
    ChannelUnavailable,
    MessageMissing,
    PermissionDenied,
    PublishError,
    SendResult,
    TransientPublishError,
)
from amir.publishing.sanitize import ZWSP, truncate

# Member/channel mentions such as "<@id>" are left intact so voice-log lines render them;
# AllowedMentions stops any ping. Mass mentions are defanged as well, belt and braces.
_MASS_MENTION = re.compile(r"@(?=(?:everyone|here))", re.IGNORECASE)


class DiscordSender:
    def __init__(self, client: discord.Client, channel_ids: Mapping[ChannelKey, int]) -> None:
        self._client = client
        self._channel_ids = dict(channel_ids)

    async def send(
        self,
        channel: ChannelKey,
        *,
        cards: Sequence[Card] = (),
        text: str | None = None,
        role_ping_id: int | None = None,
    ) -> SendResult:
        embeds = _embeds(cards)
        target = await self._resolve(channel)
        try:
            message = await target.send(
                content=_content(text, role_ping_id),
                embeds=embeds,
                allowed_mentions=_allowed_mentions(role_ping_id),
            )
        except discord.HTTPException as exc:
            raise _translate(exc, not_found=ChannelUnavailable) from exc
        return SendResult("sent", message.id)

    async def edit(
        self, channel: ChannelKey, message_id: int, *, cards: Sequence[Card]
    ) -> SendResult:
        if message_id <= 0:
            raise MessageMissing(f"no message id to edit in {channel}")
        embeds = _embeds(cards)
        target = await self._resolve(channel)
        try:
            await target.get_partial_message(message_id).edit(
                embeds=embeds, allowed_mentions=discord.AllowedMentions.none()
            )
        except discord.HTTPException as exc:
            raise _translate(exc, not_found=MessageMissing) from exc
        return SendResult("sent", message_id)

    async def find_recent(
        self, channel: ChannelKey, identities: set[tuple[str, str, str]], limit: int = 50
    ) -> int | None:
        """Id of a recent message by this bot with an embed matching one of `identities`."""
        target = await self._resolve(channel)
        own_id = self._client.user.id  # type: ignore[union-attr]
        try:
            async for message in target.history(limit=limit):
                if message.author.id != own_id:
                    continue
                if any(_embed_identity(embed) in identities for embed in message.embeds):
                    return message.id
        except discord.HTTPException as exc:
            raise _translate(exc, not_found=ChannelUnavailable) from exc
        return None

    async def _resolve(self, channel: ChannelKey) -> Any:
        channel_id = self._channel_ids.get(channel, 0)
        if not channel_id:
            raise ChannelUnavailable(f"no channel id configured for {channel}")
        target = self._client.get_channel(channel_id)
        if target is None:
            try:
                target = await self._client.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden) as exc:
                raise ChannelUnavailable(f"channel for {channel} is unavailable") from exc
            except discord.HTTPException as exc:
                raise _translate(exc, not_found=ChannelUnavailable) from exc
        # A misconfigured id may name a category or a forum: nothing can be posted there.
        if not isinstance(target, discord.abc.Messageable):
            raise ChannelUnavailable(f"channel for {channel} is not a text channel")
        return target


def _embeds(cards: Sequence[Card]) -> list[discord.Embed]:
    if len(cards) > EMBEDS_PER_MESSAGE:
        raise PublishError(f"{len(cards)} cards exceed {EMBEDS_PER_MESSAGE} embeds per message")
    return [discord.Embed.from_dict(card.to_dict()) for card in cards]


def _content(text: str | None, role_ping_id: int | None) -> str | None:
    parts: list[str] = []
    if role_ping_id:
        parts.append(f"<@&{role_ping_id}>")
    if text:
        parts.append(_MASS_MENTION.sub("@" + ZWSP, text))
    if not parts:
        return None
    return truncate(" ".join(parts), CONTENT)


def _allowed_mentions(role_ping_id: int | None) -> discord.AllowedMentions:
    if not role_ping_id:
        return discord.AllowedMentions.none()
    return discord.AllowedMentions(
        everyone=False,
        users=False,
        roles=[discord.Object(id=role_ping_id)],
        replied_user=False,
    )


def _embed_identity(embed: discord.Embed) -> tuple[str, str, str]:
    stamp = embed.timestamp.isoformat() if embed.timestamp else ""
    return (embed.title or "", embed.url or "", stamp)


def _translate(exc: discord.HTTPException, *, not_found: type[PublishError]) -> PublishError:
    if isinstance(exc, discord.Forbidden):
        return PermissionDenied(str(exc))
    if isinstance(exc, discord.NotFound):
        return not_found(str(exc))
    if isinstance(exc, discord.DiscordServerError) or exc.status == 429 or exc.status >= 500:
        return TransientPublishError(str(exc))
    return PublishError(str(exc), transient=False)
