"""Amir Beckett's voice. Pure text helpers: no discord imports."""


def footer(label: str) -> str:
    return f"Amir Beckett · {label}"


def baro_warning_title() -> str:
    return "Baro Ki'Teer is expected"


def baro_arrival_title(location: str) -> str:
    return f"Baro Ki'Teer has arrived at {location}"


def baro_departure_title() -> str:
    return "Last call for Baro Ki'Teer"


def baro_stock_continued_title(part: int) -> str:
    return f"Baro Ki'Teer's wares, continued ({part})"


_CYCLE_STATE_LINES = {
    ("cetus", "day"): "It is **Day** on the Plains. Pleasant enough, and the Eidolons keep away.",
    ("cetus", "night"): "It is **Night** on the Plains. Eidolons are abroad; do mind your step.",
    ("vallis", "warm"): "The Vallis is **Warm**, briefly. Do make the most of it.",
    ("vallis", "cold"): "The Vallis is **Cold**, as is its custom.",
    ("cambion", "fass"): "**Fass** holds the Drift, for now.",
    ("cambion", "vome"): "**Vome** holds the Drift, for now.",
}

_CYCLE_CHANGES = {
    "day": "Daybreak",
    "night": "Nightfall",
    "warm": "The thaw comes",
    "cold": "The cold returns",
    "fass": "Fass takes over",
    "vome": "Vome takes over",
}


def cycle_state_line(world: str, state: str) -> str:
    """One open world's current state, as a sentence."""
    return _CYCLE_STATE_LINES[(world, state)]


def cycle_change_line(next_state: str, relative: str, clock_time: str) -> str:
    """When the next state begins; `relative` and `clock_time` are Discord timestamps."""
    return f"{_CYCLE_CHANGES[next_state]} {relative}, at {clock_time}."


def voice_join_line(member_mention: str, channel_mention: str) -> str:
    """Voice-log line for an arrival. Takes mention strings (<@id>, <#id>), never names."""
    return f"{member_mention} has arrived in {channel_mention}."


def voice_move_line(member_mention: str, from_mention: str, to_mention: str) -> str:
    """Voice-log line for a move between channels. Takes mention strings, never names."""
    return f"{member_mention} has moved from {from_mention} to {to_mention}."


def voice_leave_line(member_mention: str, channel_mention: str) -> str:
    """Voice-log line for a departure. Takes mention strings (<@id>, <#id>), never names."""
    return f"{member_mention} has left {channel_mention}. Safe travels."
