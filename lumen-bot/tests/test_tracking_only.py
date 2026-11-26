"""Lumen reports on the predictions he tracks; no alert speaks to the reader as advice."""

import json

import pytest

from formatter import PickFormatter
from game_context import AlertType
from intelligence import generate_alert_message
from tests.game_replay import replay

ADVICE_TALK = ("your",)


def _rendered(game_id: str) -> list[str]:
    """Every alert of the replayed game, rendered as Lumen sends it, at the moment it fires."""
    formatter, texts = PickFormatter(), []

    def render(alert, row):
        alert_type, game, ctx = alert
        if alert_type == AlertType.HALFTIME_REPORT:
            embed = formatter.halftime_report_embed(game)
        else:
            message = generate_alert_message(alert_type, game, ctx)
            embed = formatter.copilot_embed(alert_type, message, game, ctx)
        texts.append(json.dumps(embed.to_dict(), ensure_ascii=False))

    replay(game_id, on_alert=render)
    return texts


@pytest.mark.parametrize("game_id", ["401909955", "401909957"])
def test_no_alert_speaks_to_the_reader_as_advice(game_id):
    texts = _rendered(game_id)

    assert texts and not [t for t in texts if any(word in t.lower() for word in ADVICE_TALK)]
