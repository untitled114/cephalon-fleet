"""Which voice channel Amir sits in: a pure decision over a census of the server's channels.

The census holds member ids in memory only; nothing here is stored or logged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar


@dataclass(frozen=True)
class ChannelView:
    """One voice channel as Amir sees it.

    `candidate`: Amir may choose to sit here. `can_speak`: his role has speak here, so a greeting
    can play.
    """

    channel_id: int
    humans: frozenset[int]
    candidate: bool
    can_speak: bool


@dataclass(frozen=True)
class VoiceCensus:
    """Every voice channel in the server's channel order."""

    channels: tuple[ChannelView, ...]
    afk_channel_id: int | None

    EMPTY: ClassVar[VoiceCensus]

    def humans_in(self, channel_id: int) -> frozenset[int]:
        for view in self.channels:
            if view.channel_id == channel_id:
                return view.humans
        return frozenset()

    def count(self, channel_id: int) -> int:
        return len(self.humans_in(channel_id))

    def can_speak_in(self, channel_id: int) -> bool:
        """Amir's role may speak in `channel_id`, so a greeting can play there."""
        return any(view.channel_id == channel_id and view.can_speak for view in self.channels)

    def candidates_with_people(self) -> int:
        """The total of humans in candidate channels."""
        return sum(len(view.humans) for view in self.channels if view.candidate)


VoiceCensus.EMPTY = VoiceCensus(channels=(), afk_channel_id=None)


@dataclass(frozen=True)
class Choice:
    target: int | None
    settle: bool
    pinned: bool


def choose(census: VoiceCensus, *, current: int | None, pin: int | None, home: int) -> Choice:
    """Where Amir should sit: a pinned channel with people, else the busiest candidate."""
    if pin is not None and census.count(pin) > 0:
        return Choice(pin, settle=False, pinned=True)
    candidates = [view for view in census.channels if view.candidate and view.humans]
    if not candidates:
        return Choice(None, settle=False, pinned=False)
    most = max(len(view.humans) for view in candidates)
    tied = [view.channel_id for view in candidates if len(view.humans) == most]
    if current in tied:
        return Choice(current, settle=False, pinned=False)
    target = home if home in tied else tied[0]
    settle = current is not None and current != census.afk_channel_id and census.count(current) > 0
    return Choice(target, settle=settle, pinned=False)
