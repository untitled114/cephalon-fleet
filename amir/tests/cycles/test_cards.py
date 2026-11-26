from datetime import UTC, datetime

import pytest

from amir import persona
from amir.config.settings import CyclesSettings
from amir.cycles.cards import cycle_card
from amir.cycles.model import NEXT_STATE, WORLD_NAMES, roll_forward
from amir.publishing.cards import Card
from amir.worldstate.wfcd import parse_cycles

CAPTURE = "wfcd_pc_baro_present.json"
FOOTER = "Amir Beckett · Cycle times via warframestat.us"


@pytest.fixture
def snaps(captured):
    return parse_cycles(captured(CAPTURE))


def test_cards_from_capture(snaps):
    assert set(snaps) == set(WORLD_NAMES)
    for world, snap in snaps.items():
        card = cycle_card(snap)
        unix = int(snap.expiry.timestamp())

        assert isinstance(card, Card)
        assert card.title == WORLD_NAMES[world]
        assert f"<t:{unix}:R>" in card.description
        assert f"<t:{unix}:t>" in card.description
        # A blank line between the state line and the countdown, on all three cards.
        assert card.description == "\n\n".join(
            [
                persona.cycle_state_line(world, snap.state),
                persona.cycle_change_line(NEXT_STATE[snap.state], f"<t:{unix}:R>", f"<t:{unix}:t>"),
            ]
        )
        assert card.footer == persona.footer("Cycle times via warframestat.us") == FOOTER
        assert card.timestamp is None
        assert "<t:" not in card.title and "<t:" not in card.footer
        assert "timestamp" not in card.to_dict()


def test_card_wording_names_current_and_next_state(snaps):
    cetus = cycle_card(snaps["cetus"]).description
    vallis = cycle_card(snaps["vallis"]).description
    cambion = cycle_card(snaps["cambion"]).description

    assert "**Night**" in cetus and "Daybreak" in cetus
    assert "**Cold**" in vallis and "thaw" in vallis
    assert "**Vome**" in cambion and "Fass" in cambion


def test_every_state_is_named_with_a_capital():
    # "Day", not "day": every state reads as a name, as Fass and Vome do.
    for world, state in [(w, s) for s in NEXT_STATE for w in WORLD_NAMES]:
        try:
            line = persona.cycle_state_line(world, state)
        except KeyError:
            continue  # that state belongs to another world
        assert f"**{state.capitalize()}**" in line, line


def test_card_follows_rolled_state(snaps):
    durations = CyclesSettings().durations
    now = datetime(2026, 10, 25, 1, 35, tzinfo=UTC)
    before = cycle_card(snaps["vallis"])

    warm = roll_forward(snaps["vallis"], now, durations)
    after = cycle_card(warm)

    unix = int(warm.expiry.timestamp())
    assert after.title == before.title == "Orb Vallis"
    assert "**Warm**" in after.description and "cold" in after.description
    assert f"<t:{unix}:R>" in after.description
    assert after.description != before.description
    assert after.color != before.color


def test_every_world_state_has_a_line(snaps):
    for world, snap in snaps.items():
        for state in (snap.state, NEXT_STATE[snap.state]):
            line = persona.cycle_state_line(world, state)
            assert state.lower() in line.lower()
            assert "<t:" not in line
