"""A pick's season average, as Lumen looks it up from Lunara's players API.

Real responses only (tests/fixtures/players/2026-11-10, provenance.json): tonight's /picks/today,
one /players search per case and Josh Hart's /players/{id}/stats. A search groups its matches by
team, and Lumen read the first team as if it were the player: no pick ever had an average.
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

from formatter import PickFormatter
from ws_listener import WSListener

API = "http://127.0.0.1:8010"
FIXTURE = Path(__file__).parent / "fixtures" / "players" / "2026-11-10"


def _raw(name: str) -> bytes:
    return (FIXTURE / name).read_bytes()


def _json(name: str):
    return json.loads(_raw(name))


async def test_a_picks_season_average_comes_from_its_players_stats():
    picks = _json("picks_today.json")
    [hart] = [p for p in picks if p["player_name"] == "Josh Hart"]
    lumen = WSListener(API, "ws://127.0.0.1:8010/ws", {}, AsyncMock(), PickFormatter())
    lumen.engine.register_picks(picks)

    with respx.mock() as api:
        api.get(f"{API}/players", params={"search": "Josh Hart"}).mock(
            return_value=httpx.Response(200, content=_raw("search_Josh_Hart.json"))
        )
        api.get(f"{API}/players/3062679/stats").mock(
            return_value=httpx.Response(200, content=_raw("stats_3062679.json"))
        )
        await lumen._fetch_season_stats_for_picks([hart])

    # Tonight's pick is his rebounds: 7.3 a game this season.
    assert lumen.engine.games[hart["game_id"]].picks[hart["id"]].season_avg == 7.3


async def test_no_average_when_the_stats_lookup_fails():
    # A failure injected at the boundary: the search is real, its stats request answers 500.
    picks = _json("picks_today.json")
    [hart] = [p for p in picks if p["player_name"] == "Josh Hart"]
    lumen = WSListener(API, "ws://127.0.0.1:8010/ws", {}, AsyncMock(), PickFormatter())
    lumen.engine.register_picks(picks)

    with respx.mock() as api:
        api.get(f"{API}/players", params={"search": "Josh Hart"}).mock(
            return_value=httpx.Response(200, content=_raw("search_Josh_Hart.json"))
        )
        api.get(f"{API}/players/3062679/stats").mock(return_value=httpx.Response(500))
        await lumen._fetch_season_stats_for_picks([hart])

    assert lumen.engine.games[hart["game_id"]].picks[hart["id"]].season_avg is None


async def test_a_failed_lookup_is_tried_again_on_the_next_poll():
    # A failure injected at the boundary: the first stats request answers 500, the next is real.
    picks = _json("picks_today.json")
    [hart] = [p for p in picks if p["player_name"] == "Josh Hart"]
    lumen = WSListener(API, "ws://127.0.0.1:8010/ws", {}, AsyncMock(), PickFormatter())
    lumen.engine.register_picks(picks)

    with respx.mock() as api:
        api.get(f"{API}/players", params={"search": "Josh Hart"}).mock(
            return_value=httpx.Response(200, content=_raw("search_Josh_Hart.json"))
        )
        api.get(f"{API}/players/3062679/stats").mock(
            side_effect=[
                httpx.Response(500),
                httpx.Response(200, content=_raw("stats_3062679.json")),
            ]
        )
        await lumen._fetch_season_stats_for_picks([hart])
        await lumen._fetch_season_stats_for_picks([hart])  # the next poll

    assert lumen.engine.games[hart["game_id"]].picks[hart["id"]].season_avg == 7.3


async def test_a_player_lunara_does_not_know_is_not_asked_again():
    # The search answers as it does for a name it doesn't know (Kevin Porter's real reply).
    picks = _json("picks_today.json")
    [hart] = [p for p in picks if p["player_name"] == "Josh Hart"]
    lumen = WSListener(API, "ws://127.0.0.1:8010/ws", {}, AsyncMock(), PickFormatter())
    lumen.engine.register_picks(picks)

    with respx.mock() as api:
        search = api.get(f"{API}/players", params={"search": "Josh Hart"}).mock(
            return_value=httpx.Response(200, content=_raw("search_Kevin_Porter.json"))
        )
        await lumen._fetch_season_stats_for_picks([hart])
        await lumen._fetch_season_stats_for_picks([hart])

    assert search.call_count == 1


@pytest.mark.parametrize(
    "response, name, player_id",
    [
        ("search_Josh_Hart.json", "Josh Hart", "3062679"),
        ("search_Jaren_Jackson.json", "Jaren Jackson", "4277961"),  # the one match: "... Jr."
        ("search_Williams.json", "Williams", None),  # six players, none named "Williams"
        ("search_Kevin_Porter.json", "Kevin Porter", None),
    ],
)
def test_a_search_names_the_player_only_when_it_is_sure(response, name, player_id):
    from ws_listener import _searched_player

    player = _searched_player(_json(response), name)

    assert (player or {}).get("id") == player_id
