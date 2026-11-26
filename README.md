# Cephalon Fleet

Real-time data services from a personal sports-data platform, delivered through Discord. Two of
them live here, each its own project with its own tests; one script defines "passing" for both, and
CI runs it on every push. The predictions Lumen follows come from
[sport-suite](https://github.com/untitled114/sport-suite), a data platform for NBA and MLB player
statistics.

| Service | What it does |
|---|---|
| [Lumen](lumen-bot/) | A stream processor for live NBA games: one WebSocket per tracked game plus the scoreboard's, per-game state, live pace projections and game-state signals, event-driven alerts, and a JSON-lines event log for every night. |
| [Amir](amir/) | Ingestion for a Warframe community: polls the official news feeds and the game's world state, deduplicates what it reads, and delivers new items through a durable outbox; he also follows voice activity and greets arrivals with recorded clips. |

The platform has more services than these two (see What's next). The AI layer behind the services'
conversations stays private.

## What's next

- **Axiom**: the machinery of the platform's analyst: posts claimed so none goes out twice,
  de-duplication, an operator-aware scheduler, season and schedule detection from ESPN, injury
  intake, and the registry his private rules plug into. Next release.
- **Atlas**: a monitor for the whole fleet. He watches every service, investigates failures
  read-only (logs, the database, the deployed code) and reports what he finds. Joins once he's live.
- **Solace**: a monitoring and briefing service: health checks on external connectors, a scheduled
  pre-open check and end-of-day reports.

## The data engineering in it

- **Streaming ingestion with state.** Lumen keeps a WebSocket open for each game with a prediction
  still open, and one for the scoreboard, reconnects when one drops, and folds every update into
  per-game state, from which pace projections and game-state signals are derived as the games run.
- **Event logs that replay.** Each change of a tracked game is one JSON line in that night's log.
  The tests replay recorded games through the engine from those logs.
- **Polling, deduplication and transactional writes.** Amir polls each news source on its own
  interval, and the game's world state on another. A poll's fetch and parse run with no transaction
  open; then everything it learned (new items, the source's state, the posts to send) commits in one
  transaction, and items are deduplicated by their stable ids.
- **Durable delivery.** Posts wait in a SQLite outbox. One counts as sent only after Discord
  confirms it, and one interrupted mid-send is resolved at startup by looking for it in the channel
  before anything is sent again.
- **Real captures as fixtures.** Every fixture comes from a real source (the platform's API and
  streams on game nights, Warframe's official feeds and world state). Some are cut to the rows a
  test needs, and the public copies leave a few fields out; each folder has a note saying where and
  when it was recorded, and what was cut where something was. Amir's voice tests play NASA's
  public-domain recording of Neil Armstrong's first words on the Moon.
- **One definition of passing.** `scripts/check.sh` builds each project's environment from pinned
  requirements on a uv-managed Python, runs its tests with a 96% coverage gate (a skipped test fails
  the run), then ruff. CI runs the same script on a clean checkout, with its actions pinned by
  commit.
- **Optional parts stay optional.** Lumen's conversational layer is a separate component: he loads
  it only where it's installed and runs on his rules without it, as he does here.

## Running the checks

Run them from a git clone (Amir's packaging tests read the repository's git index). Needs
[uv](https://docs.astral.sh/uv/), FFmpeg (Amir's voice tests decode audio) and rsync (his deploy
script's tests).

```bash
scripts/check.sh                 # both projects
scripts/check.sh --stage amir    # or one of them
scripts/check.sh --stage lumen
```

## Layout

```
amir/        Amir's project: his README and docs/architecture-and-reasoning.md explain it
lumen-bot/   Lumen's project, with his README
scripts/     check.sh, and the pytest plugin that fails a run on a skipped test
```

## Licence

MIT: see [LICENSE](LICENSE).
