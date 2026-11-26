from datetime import UTC, datetime, timedelta

import pytest

from amir.config.settings import CyclesSettings
from amir.cycles.model import NEXT_STATE, WORLD_NAMES, roll_forward
from amir.worldstate.wfcd import parse_cycles

CAPTURE = "wfcd_pc_baro_present.json"
DURATIONS = CyclesSettings().durations


def at(hour: int, minute: int, second: int = 0) -> datetime:
    return datetime(2026, 10, 25, hour, minute, second, tzinfo=UTC)


@pytest.fixture
def snaps(captured):
    return parse_cycles(captured(CAPTURE))


def test_world_names_and_next_state():
    assert WORLD_NAMES == {
        "cetus": "Plains of Eidolon",
        "vallis": "Orb Vallis",
        "cambion": "Cambion Drift",
    }
    assert NEXT_STATE == {
        "day": "night",
        "night": "day",
        "warm": "cold",
        "cold": "warm",
        "fass": "vome",
        "vome": "fass",
    }


def test_roll_vallis(snaps):
    vallis = snaps["vallis"]
    assert (vallis.state, vallis.expiry) == ("cold", at(1, 33, 28))

    warm = roll_forward(vallis, at(1, 35), DURATIONS)
    assert (warm.world, warm.state, warm.expiry) == ("vallis", "warm", at(1, 40, 8))

    cold = roll_forward(vallis, at(1, 41), DURATIONS)
    assert (cold.world, cold.state, cold.expiry) == ("vallis", "cold", at(2, 0, 8))
    assert roll_forward(warm, at(1, 41), DURATIONS) == cold


def test_roll_cetus(snaps):
    cetus = snaps["cetus"]
    assert (cetus.state, cetus.expiry) == ("night", at(1, 42))

    day = roll_forward(cetus, at(1, 43), DURATIONS)

    assert (day.world, day.state, day.expiry) == ("cetus", "day", at(3, 22))


def test_roll_before_expiry_keeps_snapshot(snaps):
    for snap in snaps.values():
        assert roll_forward(snap, at(1, 21), DURATIONS) == snap
        assert roll_forward(snap, snap.expiry - timedelta(seconds=1), DURATIONS) == snap


def test_roll_at_expiry_instant_moves_on(snaps):
    cambion = snaps["cambion"]

    rolled = roll_forward(cambion, cambion.expiry, DURATIONS)

    assert rolled.state == "fass"
    assert rolled.expiry == cambion.expiry + timedelta(seconds=DURATIONS["fass"])


def test_roll_many_phases(snaps):
    six_hours_later = at(7, 21)

    rolled = {w: roll_forward(snap, six_hours_later, DURATIONS) for w, snap in snaps.items()}

    assert (rolled["cetus"].state, rolled["cetus"].expiry) == ("day", at(8, 22))
    assert (rolled["vallis"].state, rolled["vallis"].expiry) == ("warm", at(7, 26, 48))
    assert (rolled["cambion"].state, rolled["cambion"].expiry) == ("fass", at(8, 22))
