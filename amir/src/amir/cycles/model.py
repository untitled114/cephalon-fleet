"""Cycle states and roll-forward. Pure: no I/O.

A snapshot says which state a world is in and when that state ends. Between fetches the state is
rolled forward locally from configured phase lengths, so cards flip on time without polling.
Lengths come only from expiries: WFCD's `activation` is rounded down and is never used.
"""

from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timedelta

from amir.worldstate.wfcd import CycleSnapshot

__all__ = ["NEXT_STATE", "WORLD_NAMES", "CycleSnapshot", "roll_forward"]

WORLD_NAMES = {"cetus": "Plains of Eidolon", "vallis": "Orb Vallis", "cambion": "Cambion Drift"}
NEXT_STATE = {
    "day": "night",
    "night": "day",
    "warm": "cold",
    "cold": "warm",
    "fass": "vome",
    "vome": "fass",
}


def roll_forward(snap: CycleSnapshot, now: datetime, durations: Mapping[str, int]) -> CycleSnapshot:
    """The snapshot as it stands at `now`: each state ends at its expiry, at that very instant.

    `durations` holds each state's length in seconds; they must be positive, as
    `CyclesSettings` guarantees. A snapshot that has not expired comes back equal.
    """
    state, expiry = snap.state, snap.expiry
    while expiry <= now:
        state = NEXT_STATE[state]
        expiry += timedelta(seconds=durations[state])
    return replace(snap, state=state, expiry=expiry)
