"""Replay a real game, as Lumen's own game log recorded it, through the context engine.

tests/fixtures/game_logs/2026-11-09 holds two games from Lumen's log in production
(provenance.json says how they were cut): every row where the game or a pick changed, with
the clock, score and status Lumen saw, and each pick's value, result and box-score minutes.
A replay feeds them back in order: box-score minutes and pick values when they changed (the
feed sends a pick update only on a change), then the game update.
"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

from game_context import AlertType, GameContextEngine

GAME_LOGS = Path(__file__).parent / "fixtures" / "game_logs" / "2026-11-09"


def rows(game_id: str, day: str = "2026-11-09") -> list[dict]:
    with gzip.open(GAME_LOGS.parent / day / f"game_context_{game_id}.jsonl.gz", "rt") as f:
        return [json.loads(line) for line in f]


def alerts_sent(game_id: str) -> list[dict]:
    return json.loads((GAME_LOGS / "alerts_sent.json").read_text())[game_id]


def _pick(row: dict, p: dict) -> dict:
    return {
        "id": p["pick_id"],
        "game_id": row["game_id"],
        "home_team": row["home_team"],
        "away_team": row["away_team"],
        **{k: p[k] for k in ("player_name", "team", "opponent_team", "market", "line")},
        **{k: p[k] for k in ("prediction", "tier", "model_version", "is_home")},
    }


def _box_score(p: dict) -> dict:
    return {
        "name": p["player_name"],
        "minutes": str(p["player_minutes"]),
        "fouls": p["fouls"],
        "fg": f"{p['fg_made']}-{p['fg_attempted']}",
        "three_pt": f"{p['three_made']}-{p['three_attempted']}",
        "ft": f"{p['ft_made']}-{p['ft_attempted']}",
        "plus_minus": str(p["plus_minus"]),
        "starter": p["starter"],
    }


def replay(
    game_id: str,
    engine: GameContextEngine | None = None,
    until=None,
    stop_before=None,
    on_alert=None,
    day: str = "2026-11-09",
):
    """Every alert the engine raises over the game, with the row that raised it.

    on_alert(alert, row), if given, sees each alert as it is raised, while the game stands
    where it stood then.
    """
    engine = engine or GameContextEngine()
    raised, seen = _Raised(on_alert), {}
    for row in rows(game_id, day):
        if row["event"] != "game_update":
            continue  # the alerts Lumen logged are what a replay must produce, not input
        if stop_before and stop_before(row):
            break
        for p in row["picks"]:
            if p["pick_id"] not in seen:
                engine.register_picks([_pick(row, p)])
            before = seen.get(p["pick_id"], {})
            box = ("player_minutes", "fouls", "fg_made", "fg_attempted", "plus_minus")
            if any(p[k] != before.get(k) for k in box) and p["player_minutes"]:
                for alert in engine.update_box_score(game_id, [_box_score(p)]):
                    raised.append((alert, row))
            if (p["actual_value"], p["is_hit"]) != (
                before.get("actual_value"),
                before.get("is_hit"),
            ):
                update = {"id": p["pick_id"], "game_id": game_id}
                update |= {"actual_value": p["actual_value"], "is_hit": p["is_hit"]}
                for alert in engine.update_picks([update]):
                    raised.append((alert, row))
            seen[p["pick_id"]] = p
        game = {k: row[k] for k in ("home_team", "away_team", "home_score", "away_score")}
        game |= {"id": game_id, "status": row["status"], "quarter": row["quarter"]}
        game["clock"] = row["clock"]
        for alert in engine.update_game(game):
            raised.append((alert, row))
        if until and until(row):
            break
    return list(raised)


class _Raised(list):
    def __init__(self, on_alert):
        super().__init__()
        self._on_alert = on_alert

    def append(self, item):
        super().append(item)
        if self._on_alert:
            self._on_alert(*item)


def kinds(raised, game_id: str) -> list[tuple[str, str]]:
    """(alert type, its pick's player or the game id), as Lumen's journal names them."""
    return [(a.name, ctx.player_name if ctx else game_id) for (a, _game, ctx), _row in raised]


ENGINE_KINDS = {a.name for a in AlertType}


def feed_pick_updates(game_id: str) -> list[list[dict]]:
    """The pick_update messages Lunara sent for the game, as recorded live (provenance.json)."""
    with (GAME_LOGS / f"feed_{game_id}_pick_updates.jsonl").open() as f:
        return [json.loads(json.loads(line)["raw"])["data"]["picks"] for line in f]


def register_from_log(engine: GameContextEngine, game_id: str) -> None:
    """The game's picks, as Lumen registered them that night."""
    first = rows(game_id)[0]
    engine.register_picks([_pick(first, p) for p in first["picks"]])


def feed_game_updates(game_id: str) -> list[dict]:
    """The game_update messages Lunara sent for the game from 22:02 on, as recorded live."""
    with gzip.open(GAME_LOGS / f"feed_{game_id}_game_updates.jsonl.gz", "rt") as f:
        return [json.loads(json.loads(line)["raw"])["data"] for line in f]


def feed_scoreboard_update() -> list[dict]:
    """One scoreboard_update as recorded live: every game of the slate, picks or not."""
    return json.loads(json.loads((GAME_LOGS / "feed_scoreboard_update.json").read_text())["raw"])[
        "data"
    ]
