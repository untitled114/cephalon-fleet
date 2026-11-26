"""Classify a voice-state change as a join, leave or move between channels (pure)."""

from enum import StrEnum


class VoiceEventKind(StrEnum):
    JOIN = "join"
    LEAVE = "leave"
    MOVE = "move"


def classify(
    *,
    member_id: int,
    is_bot: bool,
    own_id: int,
    before_channel_id: int | None,
    after_channel_id: int | None,
) -> VoiceEventKind | None:
    """Return JOIN, LEAVE or MOVE when a human changed voice channel, else None (bots, Amir
    himself, and changes within one channel such as mute, deafen, stream or camera)."""
    if is_bot or member_id == own_id or before_channel_id == after_channel_id:
        return None
    if before_channel_id is None:
        return VoiceEventKind.JOIN
    if after_channel_id is None:
        return VoiceEventKind.LEAVE
    return VoiceEventKind.MOVE
