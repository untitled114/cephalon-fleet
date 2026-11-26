"""Three living cycle cards in the cycles channel, each edited in place when its world changes.

Each world is its own message. A tick rolls every world forward and touches only the worlds whose
state or expiry differs from what the card last showed (kept in SQLite, so a restart reuses the
cards and posts nothing). While publishing is off, every write becomes a preview and only the
shown state is stored. Once it is on, a tick or `ensure_cards` posts each missing card exactly
once, whichever runs first; they take turns, so concurrent callers cannot double-post a card.

One world's failure never stops the others. It is logged and that world backs off, from
`RETRY_BASE_SECONDS` doubling up to `RETRY_MAX_SECONDS` and counted from the end of the failed
attempt, until a pass confirms its card is current: a tick that writes the card, or finds it
already showing the current state (a failed send or edit always leaves the stored state stale,
so that cannot hide a pending write). `ensure_cards` finding that a card exists confirms
nothing, so it leaves a streak as it is. The first failure of a streak is a warning, and so is
the first one at least `REWARN_AFTER` after the last warning; `failing()` lists the worlds in a
streak for the admin reports. A card that
was posted but whose message id could not be stored is remembered in memory and its id saved on
a later attempt: it is never posted again.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timedelta

from amir.clock import Clock
from amir.cycles.cards import cycle_card
from amir.cycles.model import NEXT_STATE, CycleSnapshot, roll_forward
from amir.publishing.gate import ChannelKey, GatedWriter, MessageMissing, PublishError
from amir.storage.repos import CycleCardRepo

log = logging.getLogger(__name__)

RETRY_BASE_SECONDS = 5
RETRY_MAX_SECONDS = 300
REWARN_AFTER = timedelta(hours=1)


class CycleService:
    def __init__(
        self,
        writer: GatedWriter,
        repo: CycleCardRepo,
        *,
        worlds: Sequence[str],
        durations: Mapping[str, int],
        clock: Clock,
    ) -> None:
        self._writer = writer
        self._repo = repo
        self._worlds = tuple(worlds)
        self._durations = dict(durations)
        self._clock = clock
        self._snaps: dict[str, CycleSnapshot] = {}
        self._lock = asyncio.Lock()
        # Posted cards whose (message_id, state, expiry) is not stored yet, by world.
        self._unsaved: dict[str, tuple[int, str, datetime]] = {}
        # Backoff after a failure, by world: the current delay and when to try again.
        self._delay: dict[str, int] = {}
        self._retry_at: dict[str, datetime] = {}
        # During a failure streak, by world: the last error's type and the last warning.
        self._error: dict[str, str] = {}
        self._warned_at: dict[str, datetime] = {}

    async def apply_snapshots(self, snaps: Mapping[str, CycleSnapshot]) -> None:
        """Take fresh snapshots for configured worlds, ignoring any older than what we hold."""
        now = self._clock.now()
        for world in self._worlds:
            snap = snaps.get(world)
            if snap is None:
                continue
            if not self._rollable(snap.state):
                log.warning("cycle snapshot ignored world=%s: unknown state", world)
                continue
            held = self._snaps.get(world)
            if held is not None and snap.expiry < roll_forward(held, now, self._durations).expiry:
                log.debug("cycle snapshot ignored world=%s: older than the rolled one", world)
                continue
            self._snaps[world] = snap

    async def tick(self) -> None:
        """Roll every world forward; update the cards of worlds whose state or expiry changed.

        With publishing on, a world without a card gets one whether or not it changed.
        """
        await self._each_world(self._update)

    async def ensure_cards(self) -> None:
        """With publishing on, post a card for every world that has none yet."""
        if not self._writer.enabled:
            return
        await self._each_world(self._ensure)

    def current(self) -> list[CycleSnapshot]:
        """Every world held so far, rolled to now, in configured world order."""
        return list(self._rolled().values())

    def failing(self) -> dict[str, tuple[str, datetime]]:
        """Worlds whose card is failing to update: the last error's type and when it is tried
        next, in configured world order."""
        return {w: (self._error[w], self._retry_at[w]) for w in self._worlds if w in self._error}

    def next_change_at(self) -> datetime | None:
        """The soonest moment any world changes state, or None before the first snapshot."""
        expiries = [snap.expiry for snap in self._rolled().values()]
        return min(expiries, default=None)

    async def _each_world(self, step: Callable[[CycleSnapshot], Awaitable[bool]]) -> None:
        """Run `step` for every world not backing off. A step returns whether it confirmed the
        world's card is current."""
        async with self._lock:
            for world, snap in self._rolled().items():
                retry_at = self._retry_at.get(world)
                if retry_at is not None and self._clock.now() < retry_at:
                    continue
                try:
                    stored = await self._save_unsaved(world)
                    current = await step(snap)
                except Exception as exc:
                    # The clock after the attempt: a slow failure still backs off in full.
                    self._failed(world, exc, self._clock.now())
                else:
                    if stored or current:
                        self._succeeded(world)

    async def _update(self, snap: CycleSnapshot) -> bool:
        message_id, last_state, last_expiry = await self._repo.get(snap.world)
        changed = (last_state, last_expiry) != (snap.state, snap.expiry)
        if message_id is None:
            if changed or self._writer.enabled:
                await self._post(snap)
        elif changed:
            await self._edit(message_id, snap)
        # Written now, or already showing this state: either way the card is current.
        return True

    async def _ensure(self, snap: CycleSnapshot) -> bool:
        """Post a card for a world without one. Finding a card says nothing about its state."""
        message_id, _, _ = await self._repo.get(snap.world)
        if message_id is None:
            await self._post(snap)
            return True
        return False

    def _rolled(self) -> dict[str, CycleSnapshot]:
        """Every held snapshot rolled to now, in configured world order."""
        now = self._clock.now()
        self._snaps = {
            world: roll_forward(self._snaps[world], now, self._durations)
            for world in self._worlds
            if world in self._snaps
        }
        return dict(self._snaps)

    def _rollable(self, state: str) -> bool:
        return state in NEXT_STATE and {state, NEXT_STATE[state]} <= self._durations.keys()

    async def _post(self, snap: CycleSnapshot) -> None:
        result = await self._writer.send(ChannelKey.CYCLES, cards=[cycle_card(snap)])
        if result.status != "sent" or result.message_id is None:
            await self._repo.save_state(snap.world, snap.state, snap.expiry)
            return
        # Remember the card before storing it: if the store fails, it must not be posted again.
        self._unsaved[snap.world] = (result.message_id, snap.state, snap.expiry)
        try:
            await self._save_unsaved(snap.world)
        except Exception:
            log.error(
                "cycle card posted but its id was not stored world=%s message_id=%s;"
                " will store it, not repost",
                snap.world,
                result.message_id,
            )
            raise
        log.info("cycle card posted world=%s state=%s", snap.world, snap.state)

    async def _save_unsaved(self, world: str) -> bool:
        """Store the id of a posted card that could not be stored before; True if there was one.

        Storing it completes that post, so it counts as a write.
        """
        unsaved = self._unsaved.get(world)
        if unsaved is None:
            return False
        await self._repo.save(world, *unsaved)
        del self._unsaved[world]
        return True

    async def _edit(self, message_id: int, snap: CycleSnapshot) -> None:
        try:
            await self._writer.edit(ChannelKey.CYCLES, message_id, cards=[cycle_card(snap)])
        except MessageMissing:
            log.info("cycle card missing world=%s; posting a fresh one", snap.world)
            await self._post(snap)
            return
        await self._repo.save_state(snap.world, snap.state, snap.expiry)
        log.debug("cycle card edited world=%s state=%s", snap.world, snap.state)

    def _failed(self, world: str, exc: Exception, now: datetime) -> None:
        first = world not in self._delay
        delay = RETRY_BASE_SECONDS if first else min(self._delay[world] * 2, RETRY_MAX_SECONDS)
        self._delay[world] = delay
        self._retry_at[world] = now + timedelta(seconds=delay)
        self._error[world] = type(exc).__name__
        # The first failure in a row is a warning, then at most one an hour while it lasts. A
        # publish failure is expected weather; anything else may be a bug, so keep its trace.
        warn = first or now - self._warned_at[world] >= REWARN_AFTER
        if warn:
            self._warned_at[world] = now
        log.log(
            logging.WARNING if warn else logging.DEBUG,
            "cycle card update failed world=%s error=%s; retrying in %ss",
            world,
            type(exc).__name__,
            delay,
            exc_info=not isinstance(exc, PublishError),
        )

    def _succeeded(self, world: str) -> None:
        if self._delay.pop(world, None) is not None:
            del self._retry_at[world], self._error[world], self._warned_at[world]
            log.info("cycle card recovered world=%s", world)
