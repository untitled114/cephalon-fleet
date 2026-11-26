"""Lumen's game log keeps what changed in the games it tracks, not every repeat of every game."""

import json
from unittest.mock import AsyncMock

from formatter import PickFormatter
from game_log import GameLogRecorder
from tests.game_replay import feed_game_updates, feed_scoreboard_update, register_from_log
from ws_listener import WSListener

DEN_AT_HOU = "401909957"


def _listener(tmp_path) -> WSListener:
    listener = WSListener(
        api_url="http://127.0.0.1:8010",
        ws_url="ws://127.0.0.1:8010/ws",
        alert_cfg={},
        send_dm=AsyncMock(),
        formatter=PickFormatter(),
    )
    listener.game_log = GameLogRecorder(str(tmp_path))
    register_from_log(listener.engine, DEN_AT_HOU)
    return listener


def _written(tmp_path) -> list[dict]:
    return [json.loads(line) for path in tmp_path.glob("*.jsonl") for line in path.open()]


async def test_a_repeated_game_update_is_logged_once(tmp_path):
    listener = _listener(tmp_path)
    updates = feed_game_updates(DEN_AT_HOU)

    for update in updates:
        await listener._handle_game_update(update)

    # 1,451 updates from 22:02 to the end; 126 of them changed the clock, score or status.
    assert (len(updates), len(_written(tmp_path))) == (1451, 126)


async def test_only_the_games_lumen_tracks_are_logged(tmp_path):
    listener = _listener(tmp_path)

    for game in feed_scoreboard_update():  # all 8 games of the slate
        await listener._handle_game_update(game)

    assert {row["game_id"] for row in _written(tmp_path)} == {DEN_AT_HOU}
