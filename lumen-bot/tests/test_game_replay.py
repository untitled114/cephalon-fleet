"""Lumen's engine over two real games, and how its alerts differ from what it sent that night.

The replay itself was checked against the night first: the engine Lumen ran
on 2026-11-09 raised, in order, exactly the alerts its journal logged.
"""

import pytest

from tests.game_replay import ENGINE_KINDS, alerts_sent, kinds, replay

NIGHT_TO_NOW = {
    "401909955": [  # MEM @ TOR
        ("QUARTER_SUMMARY", "401909955"),
        # Edey's came at 5 minutes played, "on pace for 36"; Walter's once he had 11 minutes.
        ("PACE_COMFORT", "Ja'Kobe Walter"),
        ("HALFTIME_REPORT", "401909955"),
        ("PACE_COMFORT", "Zach Edey"),  # 6 rebounds in 12 minutes
        ("LINE_CLEARED_EARLY", "Ja'Kobe Walter"),
        # The night's end-of-Q2 summary, after the halftime report, is gone.
        ("QUARTER_SUMMARY", "401909955"),
        ("LINE_CLEARED_EARLY", "Zach Edey"),
    ],
    "401909957": [  # DEN @ HOU
        ("QUARTER_SUMMARY", "401909957"),
        ("PACE_COMFORT", "Aaron Gordon"),  # 2 rebounds at 10 minutes played
        ("HALFTIME_REPORT", "401909957"),
        # Still 2 at 19 minutes: behind pace, where the night kept saying "crushing".
        ("PACE_CONCERN", "Aaron Gordon"),
        ("LINE_CLEARED_EARLY", "Aaron Gordon"),
        ("QUARTER_SUMMARY", "401909957"),
    ],
}


@pytest.mark.parametrize("game_id", NIGHT_TO_NOW)
def test_the_engine_over_the_night(game_id):
    assert kinds(replay(game_id), game_id) == NIGHT_TO_NOW[game_id]


@pytest.mark.parametrize("game_id", NIGHT_TO_NOW)
def test_the_fixture_holds_the_alerts_lumen_sent_that_night(game_id):
    sent = [a["kind"] for a in alerts_sent(game_id)]
    assert {"HALFTIME_REPORT", "LINE_CLEARED_EARLY", "MID_GAME_HIT", "RESULT"} <= set(sent)
    assert set(sent) - {"MID_GAME_HIT", "RESULT"} <= ENGINE_KINDS
