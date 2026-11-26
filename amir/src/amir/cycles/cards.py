"""One living card per open world. Pure: no I/O, no discord imports.

The card carries no embed timestamp: it is edited in place and found again by its stored
message id, never by reconciliation. Discord timestamps appear only in the description, where
the relative one keeps counting down on every client between edits.
"""

from amir import persona
from amir.cycles.model import NEXT_STATE, WORLD_NAMES, CycleSnapshot
from amir.publishing.cards import Card

FOOTER_LABEL = "Cycle times via warframestat.us"
STATE_COLORS = {
    "day": 0xE0B04A,
    "night": 0x2C3E66,
    "warm": 0xD9822B,
    "cold": 0x5DA9E9,
    "fass": 0xB5653A,
    "vome": 0x4A6FA5,
}


def cycle_card(snap: CycleSnapshot) -> Card:
    unix = int(snap.expiry.timestamp())
    # A blank line sets the countdown apart from the state line.
    description = "\n\n".join(
        [
            persona.cycle_state_line(snap.world, snap.state),
            persona.cycle_change_line(NEXT_STATE[snap.state], f"<t:{unix}:R>", f"<t:{unix}:t>"),
        ]
    )
    return Card(
        title=WORLD_NAMES[snap.world],
        description=description,
        color=STATE_COLORS[snap.state],
        timestamp=None,
        footer=persona.footer(FOOTER_LABEL),
    )
