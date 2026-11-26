"""One message per moment: halftime once, and each line crossing once."""

from unittest.mock import AsyncMock

import pytest

import intelligence
from formatter import PickFormatter
from game_context import GameContextEngine
from tests.game_replay import feed_pick_updates, kinds, replay
from ws_listener import WSListener

GAMES = ["401909955", "401909957"]  # MEM @ TOR and DEN @ HOU, 2026-11-09


@pytest.mark.parametrize("game_id", GAMES)
def test_the_halftime_report_is_not_followed_by_an_end_of_q2_summary(game_id):
    raised = replay(game_id)

    summaries = [row["quarter"] for (a, _, _), row in raised if a.name == "QUARTER_SUMMARY"]
    assert ("HALFTIME_REPORT", game_id) in kinds(raised, game_id)
    assert summaries == [2, 4]  # end of Q1 and end of Q3; halftime covers the end of Q2


async def test_a_line_crossing_is_one_message(monkeypatch):
    # Lunara's feed for DEN @ HOU: Gordon at 4.0 (22:06:16), then 5.0 over 4.5 (22:16:16).
    listener = WSListener(
        api_url="http://127.0.0.1:8010",
        ws_url="ws://127.0.0.1:8010/ws",
        alert_cfg={},
        send_dm=AsyncMock(),
        formatter=PickFormatter(),
    )
    replay(
        "401909957", listener.engine, stop_before=lambda row: row["picks"][0]["actual_value"] >= 5
    )
    sent = []

    async def queue(content=None, embed=None):
        sent.append(embed.title if embed else content)

    monkeypatch.setattr(listener, "_queue_dm", queue)
    at_four, at_five, _final = feed_pick_updates("401909957")
    await listener._handle_pick_updates(at_four)
    sent.clear()

    await listener._handle_pick_updates(at_five)

    assert sent == ["Early Clear | DEN @ HOU"]


def _gordon_at(until):
    engine = GameContextEngine()
    replay("401909957", engine, until=until)
    game = engine.games["401909957"]
    return game, game.picks[1]


def test_a_crossing_reads_its_half_point():
    game, gordon = _gordon_at(lambda row: row["picks"][0]["actual_value"] >= 5)

    assert "At **5** (+0.5 over the line)" in intelligence.generate_line_cleared_early(game, gordon)


def test_a_comfortable_pace_reads_its_margin_to_the_tenth():
    # The comfort alert Gordon gets once he has 10 minutes played: his projected gap, unrounded.
    [moment] = [
        (row["quarter"], row["clock"], row["picks"][0]["player_minutes"])
        for (a, _, ctx), row in replay("401909957")
        if a.name == "PACE_COMFORT"
    ]
    game, gordon = _gordon_at(
        lambda row: (row["quarter"], row["clock"], row["picks"][0]["player_minutes"]) == moment
    )

    gap = round(gordon.latest_pace - gordon.line, 1)
    assert gap != round(gap)  # this one has a tenth to lose
    assert f"Projected **{gap:g}** above the line" in intelligence.generate_pace_comfort(
        game, gordon
    )
