"""Baro milestone schedule with catch-up semantics (pure)."""

from datetime import datetime, timedelta
from enum import StrEnum

from amir.worldstate.official import BaroVisit


class Milestone(StrEnum):
    WARNING_72H = "warn72"
    ARRIVAL = "arrival"
    DEPARTURE_10H = "depart10"


def milestone_times(
    visit: BaroVisit, *, warn_hours: int = 72, depart_hours: int = 10
) -> dict[Milestone, datetime]:
    return {
        Milestone.WARNING_72H: visit.activation - timedelta(hours=warn_hours),
        Milestone.ARRIVAL: visit.activation,
        Milestone.DEPARTURE_10H: visit.expiry - timedelta(hours=depart_hours),
    }


def due_milestones(
    visit: BaroVisit,
    now: datetime,
    already: set[Milestone],
    *,
    warn_hours: int = 72,
    depart_hours: int = 10,
) -> list[Milestone]:
    times = milestone_times(visit, warn_hours=warn_hours, depart_hours=depart_hours)
    order = [Milestone.WARNING_72H, Milestone.ARRIVAL, Milestone.DEPARTURE_10H]
    boundaries = [times[Milestone.ARRIVAL], times[Milestone.DEPARTURE_10H], visit.expiry]
    due = [
        m
        for m, nxt in zip(order, boundaries, strict=True)
        if m not in already and now >= times[m] and now < nxt
    ]
    return due[-1:]
