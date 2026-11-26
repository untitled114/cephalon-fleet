"""A pick's pace follows the clock and the player's minutes, not only his last stat change."""

import pytest

from game_context import GameContextEngine
from tests.game_replay import replay

DEN_AT_HOU = "401909957"


def test_a_stat_standing_still_slows_the_pace():
    # Gordon had 2 rebounds at 9 minutes played (pace 8) and still 2 when halftime began,
    # after 15 (the halftime report he got read "2 at half → pace 8 (15min)").
    engine = GameContextEngine()
    replay(DEN_AT_HOU, engine, until=lambda row: row["status"] == "halftime")
    gordon = engine.games[DEN_AT_HOU].picks[1]

    assert (gordon.actual_value, gordon.box_score.minutes) == (2.0, 15.0)
    assert gordon.latest_pace == 4.8  # 2 / 15 * 36
    assert gordon.comfort_level == "on_track"


@pytest.mark.parametrize("game_id", ["401909955", "401909957"])
def test_no_pace_alert_from_under_ten_minutes_played(game_id):
    # Live, Edey's comfort alert came at 5 minutes played: "on pace for 36" rebounds.
    paced = [
        (ctx.player_name, next(p for p in row["picks"] if p["pick_id"] == ctx.pick_id))
        for (a, _, ctx), row in replay(game_id)
        if a.name in ("PACE_COMFORT", "PACE_CONCERN")
    ]

    assert paced and all(p["player_minutes"] >= 10 for _, p in paced)
