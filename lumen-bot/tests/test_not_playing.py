"""A pick whose player is on no list of his team's box score is "not playing", not "0 … unknown".

Real data (tests/fixtures/players/2026-11-10, provenance.json): tonight's /picks/today and the
MIL @ NY box score fetched mid-game at 20:10 ET on 2026-11-10. Josh Hart, tonight's REBOUNDS 6.5
pick, is on neither team's list; Lumen's end-of-Q1 update showed him at 0 rebounds against the
OVER 6.5, with his comfort "unknown".
"""

import json
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
import respx

import intelligence
from formatter import PickFormatter
from game_context import GameContextEngine
from tests.game_replay import rows
from ws_listener import WSListener

API = "http://127.0.0.1:8010"
FIXTURE = Path(__file__).parent / "fixtures" / "players" / "2026-11-10"
MIL_AT_NY = "401909960"
HART = 645


def _json(name: str):
    return json.loads((FIXTURE / name).read_bytes())


def _box_players() -> list[dict]:
    """The box score's players, each with its team, as the box-score poll passes them."""
    box = _json(f"boxscore_{MIL_AT_NY}.json")
    return [
        {**p, "team": box[side]["abbrev"]}
        for side in ("home", "away")
        for p in box[side]["players"]
    ]


def _game_after_the_box():
    engine = GameContextEngine()
    engine.register_picks(_json("picks_today.json"))
    engine.update_box_score(MIL_AT_NY, _box_players())
    return engine.games[MIL_AT_NY]


def test_a_player_missing_from_his_teams_box_is_not_playing():
    assert _game_after_the_box().picks[HART].not_playing is True


def test_the_quarter_update_says_he_is_not_playing():
    game = _game_after_the_box()

    line = intelligence._pick_line(game.picks[HART], game)

    assert line == "**Josh Hart**: not playing, OVER 6.5 rebounds, no result expected"


def test_the_halftime_text_says_he_is_not_playing():
    report = intelligence.generate_halftime_report(_game_after_the_box())

    assert "**Josh Hart** OVER 6.5 rebounds" in report
    assert "not playing, no result expected" in report
    assert "at the half" not in report


def test_the_halftime_card_says_he_is_not_playing():
    embed = PickFormatter().halftime_report_embed(_game_after_the_box())

    [field] = [f for f in embed.fields if "Josh Hart" in f.name]
    assert field.name == "Josh Hart: rebounds"
    assert field.value.startswith("not playing, no result expected\n")


async def test_the_box_score_poll_tells_the_engine_each_players_team():
    lumen = WSListener(API, "ws://127.0.0.1:8010/ws", {}, AsyncMock(), PickFormatter())
    lumen.engine.register_picks(_json("picks_today.json"))
    lumen.engine.games[MIL_AT_NY].status = "live"

    with respx.mock() as api:
        api.get(f"{API}/games/{MIL_AT_NY}/boxscore").mock(
            return_value=httpx.Response(
                200, content=(FIXTURE / f"boxscore_{MIL_AT_NY}.json").read_bytes()
            )
        )
        await lumen._poll_box_scores()

    assert lumen.engine.games[MIL_AT_NY].picks[HART].not_playing is True


def test_no_alert_of_any_kind_for_a_player_not_playing():
    # Tonight's MIL @ NY, replayed from Lumen's own game log (fixtures/game_logs/2026-11-10):
    # NY led by 24 at the half and by 20+ late, and Lumen warned "blowout" for Josh Hart, who
    # was on no list of the box score.
    from tests.game_replay import kinds, replay

    engine = GameContextEngine()
    engine.register_picks(_json("picks_today.json"))
    engine.update_box_score(MIL_AT_NY, _box_players())

    raised = kinds(replay(MIL_AT_NY, engine=engine, day="2026-11-10"), MIL_AT_NY)

    assert [kind for kind, who in raised if who == "Josh Hart"] == []  # was pace, blowout, garbage
    assert ("QUARTER_SUMMARY", MIL_AT_NY) in raised  # the game itself still reports


def test_the_engine_says_once_that_he_is_not_playing():
    # Since 2026-11-11: one DM the first time a pick's player turns out not to be playing.
    # The box score lists a side's whole active roster (bench players at 0 minutes, a DNP as
    # "--"), so a player on no list of his team is out, not yet to come on.
    engine = GameContextEngine()
    engine.register_picks(_json("picks_today.json"))

    first = engine.update_box_score(MIL_AT_NY, _box_players())
    again = engine.update_box_score(MIL_AT_NY, _box_players())

    hart = [a.name for a, _game, ctx in first if ctx and ctx.player_name == "Josh Hart"]
    assert hart == ["NOT_PLAYING"]
    assert [a for a, _game, ctx in again if ctx and ctx.player_name == "Josh Hart"] == []


async def test_one_dm_says_he_isnt_playing_tonight():
    lumen = WSListener(API, "ws://127.0.0.1:8010/ws", {}, AsyncMock(), PickFormatter())
    lumen.engine.register_picks(_json("picks_today.json"))
    # The game as Lumen first saw it live, from his own log (fixtures/game_logs/2026-11-10).
    first = rows(MIL_AT_NY, day="2026-11-10")[0]
    game = {k: first[k] for k in ("home_team", "away_team", "home_score", "away_score")}
    lumen.engine.update_game(
        game
        | {"id": MIL_AT_NY, "status": first["status"], "quarter": first["quarter"]}
        | {"clock": first["clock"]}
    )
    sent = []

    async def queue(content=None, embed=None):
        sent.append(embed)

    lumen._queue_dm = queue
    with respx.mock() as api:
        api.get(f"{API}/games/{MIL_AT_NY}/boxscore").mock(
            return_value=httpx.Response(
                200, content=(FIXTURE / f"boxscore_{MIL_AT_NY}.json").read_bytes()
            )
        )
        await lumen._poll_box_scores()
        await lumen._poll_box_scores()

    [dm] = [e for e in sent if e and "Josh Hart" in (e.description or "")]
    assert dm.title == "Not Playing | MIL @ NY"
    assert dm.description == (
        "**Josh Hart** isn't playing tonight.\nOVER 6.5 rebounds, no result expected"
    )


TIP = Path(__file__).parent / "fixtures" / "players" / "2026-11-11"


def test_at_the_tip_no_dressed_player_is_called_out():
    # WSH @ IND at the opening tip of 2026-11-11 (fixtures/players/2026-11-11, provenance.json):
    # each side already lists its whole active roster, everyone at 0 or "--" minutes, so a
    # player on no list is out, never "not on yet". Tonight's two WSH picks are on it.
    box = json.loads((TIP / "boxscore_401909966_tip.json").read_bytes())
    players = [{**p, "team": box[s]["abbrev"]} for s in ("home", "away") for p in box[s]["players"]]
    assert {p["minutes"] for p in players} <= {"0", "-", "--"}  # nobody has played yet

    engine = GameContextEngine()
    engine.register_picks(json.loads((TIP / "picks_today.json").read_bytes()))
    raised = engine.update_box_score("401909966", players)

    picks = engine.games["401909966"].picks.values()
    assert sorted(ctx.player_name for ctx in picks) == ["Alex Sarr", "Bilal Coulibaly"]
    assert [a.name for a, _game, _ctx in raised] == []
    assert not any(ctx.not_playing for ctx in picks)


@pytest.mark.parametrize(
    "as_picked, league",
    [
        # Real spellings: picks name players their own way, box scores the league's way
        # (the platform's name-matching cases; the MIL @ NY box score of 2026-11-10).
        ("Michael Porter", "Michael Porter Jr."),
        ("Jaren Jackson", "Jaren Jackson Jr."),
        ("Luka Doncic", "Luka Dončić"),
        ("Jaime Jaquez", "Jaime Jaquez Jr."),
        ("Gary Trent", "Gary Trent Jr."),
        ("Kelel Ware", "Kel'el Ware"),
    ],
)
def test_a_name_matches_however_it_is_spelled(as_picked, league):
    from game_context import name_key

    assert name_key(as_picked) == name_key(league)


def test_every_player_on_the_real_box_finds_himself_spelled_the_picks_way():
    # Each name on the MIL @ NY box, without its suffix or accents, still matches its own row:
    # a pick spelled that way is playing, never "isn't playing tonight".
    from game_context import match_player

    players = _box_players()
    for p in players:
        plain = p["name"].replace(" Jr.", "").replace("'", "")
        assert match_player(plain, p["team"], players) is p, p["name"]
