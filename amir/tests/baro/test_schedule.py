from datetime import UTC, datetime, timedelta

import pytest

from amir.baro.schedule import Milestone, due_milestones, milestone_times
from amir.worldstate.official import parse_void_trader


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(UTC)


@pytest.fixture
def visit(captured):
    return parse_void_trader(captured("official_worldstate_baro_present.json"))


def test_milestone_times(visit):
    times = milestone_times(visit)
    assert times[Milestone.ARRIVAL] == visit.activation
    assert visit.activation - times[Milestone.WARNING_72H] == timedelta(hours=72)
    assert visit.expiry - times[Milestone.DEPARTURE_10H] == timedelta(hours=10)


@pytest.mark.parametrize(
    ("now", "expected"),
    [
        ("2026-10-20T13:05:00Z", [Milestone.WARNING_72H]),
        ("2026-10-22T00:00:00Z", [Milestone.WARNING_72H]),
        ("2026-10-25T01:21:00Z", [Milestone.ARRIVAL]),
        ("2026-10-25T03:30:00Z", [Milestone.DEPARTURE_10H]),
        ("2026-10-25T13:01:00Z", []),
    ],
)
def test_due_milestones(visit, now, expected):
    assert due_milestones(visit, _at(now), set()) == expected


def test_before_first_milestone_nothing_due(visit):
    assert due_milestones(visit, _at("2026-10-01T00:00:00Z"), set()) == []


def test_already_sent_excluded(visit):
    assert due_milestones(visit, _at("2026-10-25T01:21:00Z"), {Milestone.ARRIVAL}) == []


def test_boundary_instants(visit):
    t = milestone_times(visit)
    warn, arrival, depart = (
        t[Milestone.WARNING_72H],
        t[Milestone.ARRIVAL],
        t[Milestone.DEPARTURE_10H],
    )
    eps = timedelta(microseconds=1)
    assert due_milestones(visit, warn - eps, set()) == []
    assert due_milestones(visit, warn, set()) == [Milestone.WARNING_72H]
    assert due_milestones(visit, arrival - eps, set()) == [Milestone.WARNING_72H]
    assert due_milestones(visit, arrival, set()) == [Milestone.ARRIVAL]
    assert due_milestones(visit, depart - eps, set()) == [Milestone.ARRIVAL]
    assert due_milestones(visit, depart, set()) == [Milestone.DEPARTURE_10H]
    assert due_milestones(visit, visit.expiry - eps, set()) == [Milestone.DEPARTURE_10H]
    assert due_milestones(visit, visit.expiry, set()) == []
