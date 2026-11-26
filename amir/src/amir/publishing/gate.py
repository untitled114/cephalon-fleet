"""The publishing gate: every channel write goes through GatedWriter. No discord.py imports.

Hard rule: while publishing is disabled nothing is sent, edited or deleted
in any Discord channel. A disabled GatedWriter does not even keep a reference to the real
sender, so no code path can reach it; would-be posts become preview rows instead (voice-log
lines are never persisted). The switch is fixed at construction; enabling needs a restart.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Literal, Protocol

from amir.publishing.cards import Card

if TYPE_CHECKING:
    from amir.storage.repos import PreviewAction, PreviewRepo

log = logging.getLogger(__name__)


class ChannelKey(StrEnum):
    NEWS = "news"
    CYCLES = "cycles"
    VOICE_LOG = "voice_log"


@dataclass(frozen=True)
class SendResult:
    status: Literal["sent", "previewed"]
    message_id: int | None


class PublishError(Exception):
    """A channel write failed. `transient` says whether retrying later may succeed."""

    transient: bool = False

    def __init__(self, message: str = "", *, transient: bool | None = None) -> None:
        super().__init__(message)
        if transient is not None:
            self.transient = transient


class ChannelUnavailable(PublishError):
    """The channel is unset, unknown, deleted or not visible to the bot."""


class PermissionDenied(PublishError):
    """The bot lacks permission for the write."""


class MessageMissing(PublishError):
    """The message to edit no longer exists."""


class TransientPublishError(PublishError):
    """Rate limit or Discord server error; retry later."""

    transient = True


class ChannelWriter(Protocol):
    async def send(
        self,
        channel: ChannelKey,
        *,
        cards: Sequence[Card] = (),
        text: str | None = None,
        role_ping_id: int | None = None,
    ) -> SendResult: ...

    async def edit(
        self, channel: ChannelKey, message_id: int, *, cards: Sequence[Card]
    ) -> SendResult: ...

    async def find_recent(
        self, channel: ChannelKey, identities: set[tuple[str, str, str]], limit: int = 50
    ) -> int | None: ...


_PREVIEWED = SendResult("previewed", None)


class GatedWriter:
    """ChannelWriter that only reaches `sender` when constructed with `enabled=True`."""

    def __init__(self, sender: ChannelWriter, previews: PreviewRepo, *, enabled: bool) -> None:
        # Fail closed: only the literal True enables publishing.
        self._sender: ChannelWriter | None = sender if enabled is True else None
        self._previews = previews

    @property
    def enabled(self) -> bool:
        return self._sender is not None

    async def send(
        self,
        channel: ChannelKey,
        *,
        cards: Sequence[Card] = (),
        text: str | None = None,
        role_ping_id: int | None = None,
    ) -> SendResult:
        sender = self._sender
        if sender is None:  # publishing disabled
            payload = {
                "cards": [card.to_dict() for card in cards],
                "text": text,
                "role_ping_id": role_ping_id,
            }
            await self._preview(channel, "send", payload)
            return _PREVIEWED
        return await sender.send(channel, cards=cards, text=text, role_ping_id=role_ping_id)

    async def edit(
        self, channel: ChannelKey, message_id: int, *, cards: Sequence[Card]
    ) -> SendResult:
        sender = self._sender
        if sender is None:  # publishing disabled
            payload = {"message_id": message_id, "cards": [card.to_dict() for card in cards]}
            await self._preview(channel, "edit", payload)
            return _PREVIEWED
        return await sender.edit(channel, message_id, cards=cards)

    async def find_recent(
        self, channel: ChannelKey, identities: set[tuple[str, str, str]], limit: int = 50
    ) -> int | None:
        sender = self._sender
        if sender is None:  # publishing disabled
            return None
        return await sender.find_recent(channel, identities, limit)

    async def _preview(
        self, channel: ChannelKey, action: PreviewAction, payload: dict[str, Any]
    ) -> None:
        if channel == ChannelKey.VOICE_LOG:
            # Voice data is never written to SQLite; the log line carries no member identity.
            log.info("publishing disabled: %s to %s dropped, not persisted", action, channel)
            return
        await self._previews.add(channel, action, payload)
        log.info("publishing disabled: %s to %s recorded as a preview", action, channel)
