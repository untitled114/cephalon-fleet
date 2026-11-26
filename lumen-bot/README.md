# Cephalon Lumen

Lumen is a real-time stream processor for live NBA games. Each game day he loads the day's
player-stat predictions from an HTTP API (`/picks/today`), keeps a WebSocket connection open to
the update stream of every game with a prediction still open, and to the scoreboard stream, holds each game's state as updates arrive,
derives live metrics from it, and sends event-driven alerts to his operator on Discord.

What he derives while the games run:

- Pace projections from the running box score ("Jokic has 18 at the half, on pace for 36"), and
  when a prediction nears or passes its stat target.
- Game-state signals: blowouts and garbage time, when starters are likely to sit.
- Halftime report cards and quarter summaries.
- Each prediction's result at the final, the corrections the upstream service sends when it re-checks
  a final box score, and a daily recap.
- A notice when a player in a prediction isn't in the box score.

In a DM, `status`, `picks` and `recap` answer from the same state.

## The pipeline

| Stage | Module | What it does |
|---|---|---|
| Ingest | `ws_listener.py` | Finds the day's games from the API (polled every 5 minutes), keeps one WebSocket per game it tracks plus the scoreboard's, reconnects when one drops, and routes every update. |
| State | `game_context.py` | Per-game state (score, quarter, clock, pace) and per-player running stats: projections and blowout detection. |
| Derive | `intelligence.py` | Turns a game's state and a prediction's progress into a message; only actionable or informative ones go out. |
| Deliver | `formatter.py`, `bot.py` | One Discord embed per kind of alert; the client, its DM commands and a health endpoint. |
| Record | `game_log.py` | One JSON line per change of a tracked game, in the Eastern day's file: the night's event log. |
| Configure | `settings.py` | Runtime settings: the environment overrides `config.yaml`. |

His conversational layer (`ai.py`, `brain.py`, `lumen_tools.py`) is not part of this repository. The
bot loads it only when it's installed; without it Lumen runs on his rules alone, as he does here.

## Configuration

`config.yaml` holds the upstream API and WebSocket addresses and two alert switches: the "nearing
the target" alert (with how close counts, `approach_threshold`) and the result at the final
(`hit_miss`). The environment supplies the rest:

| Variable | What it is |
|---|---|
| `DISCORD_TOKEN` | The bot's token. |
| `LUMEN_OWNER_ID` | The Discord user he reports to. |
| `LUNARA_API_URL`, `LUNARA_WS_URL` | The upstream API and WebSocket base; they override `config.yaml`. |
| `HEALTH_HOST`, `PORT` | Where the health endpoint listens. |
| `ATLAS_HEARTBEAT_URL`, `ATLAS_HEARTBEAT_SECRET` | Optional: a heartbeat to the fleet's monitor. |

## Testing on recorded streams

From the repository root, `scripts/check.sh --stage lumen` runs his tests at 96% coverage (a
skipped test fails the run), then ruff check and format. The fixtures are real recordings of the
upstream API and its streams on game nights, each folder with a `provenance.json` saying where and
when it was recorded. `tests/game_replay.py` replays recorded games through the engine from his own
event logs.

## Running

Python 3.12: `pip install .`, then `python bot.py` with the environment above. A `Dockerfile` is
included.
