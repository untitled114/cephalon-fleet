# Architecture

Why it is built this way: [architecture-and-reasoning.md](architecture-and-reasoning.md).

Amir is one Python 3.12 process: a discord.py client plus supervised background tasks on one
asyncio loop. The core holds the decisions and has no Discord imports. These cover parsing,
dedup, Baro milestones, cycle roll-forward, card building, sanitising, voice-event
classification and the greeting queue. Thin adapters connect that core to Discord (discord.py
2.7.1), HTTP (aiohttp), SQLite (aiosqlite) and FFmpeg.

## Data flow

```
 forum RSS, Steam API ─▶ NewsPipeline ──────────────┐
                                                    ├─▶ outbox (SQLite) ─▶ OutboxWorker ─┐
 official + WFCD world state ─▶ WorldStatePoller ─┬─▶ BaroService ─┘                     │
   (+ WFCD item names, cached in SQLite)          │      └─ late-stock edit ─────────────┤
                                                  └─▶ CycleService (3 living cards) ─────┤
                                                                                         ▼
 Discord voice events ─▶ classify ─▶ VoiceCompanion ─▶ VoiceAnnouncer (memory) ─▶ GatedWriter ─┬─ ON:  DiscordSender ─▶ channels
                                          │                                                    └─ OFF: previews table
                                          ├─▶ voice-follow: census ─▶ choose ─▶ VoiceSession ─▶ the busiest voice channel
                                          └─▶ GreetingQueue ─▶ Player + FFmpeg ─▶ the channel Amir is in
```

- **Pollers to outbox.** News and Baro posts are queued as rows in the SQLite `outbox`, keyed
  by a dedup key: the forum `guid`, the Steam `gid`, a canonical URL, or
  `baro:{activation}:{milestone}`. Each key is unique. A poll's writes commit in one
  transaction, so a crash keeps all of a poll or none of it. The first poll of a new source
  only records a baseline.
- **Outbox to Discord.** `OutboxWorker` delivers rows in order and retries with backoff. A row
  counts as `sent` only after Discord confirms the message. A crash during the send leaves the
  row `sending`. At the next startup `reconcile_inflight` searches the channel for the card and
  marks it sent, or requeues it.
- **Cycles** don't go through the outbox. `CycleService` edits three stored messages in place,
  one per world, and posts a card only when its message is missing.
- **Voice.** `classify` turns a voice-state change, in any voice channel, into a join, a leave,
  a move between two channels, or nothing. The companion then does three independent things:
  - It hands a voice-log line to the announcer: an arrival, a departure, or one line for a move.
    The line is held in memory only and never stored or logged.
  - It keeps each person's stay in voice (`StayTracker`, in memory only) and offers a greeting to
    the bounded queue when someone owed one comes into Amir's channel: 5 jobs, each dropped once
    it is 30 s old. The greeting worker never connects: it plays one clip at a time, through
    FFmpeg's loudness filter, in the channel Amir is in, to someone still there.
  - It asks **voice-follow** to decide again where Amir sits. That one background task reads a
    census of every voice channel (`read_census`: who is where, and which channels he may sit
    in) and `choose` picks the channel with a drag's pin and people in it, else the busiest
    candidate. A bigger group elsewhere must keep its lead for the settle time (30 s) while his
    own channel has people. `VoiceSession` moves him by leaving and then joining, never with
    discord.py's `move_to`, so he stays deafened; with nobody left, it leaves after 60 s.

  When Amir joins a channel, whoever there joined voice within 30 s and is owed a greeting is
  greeted; everyone there counts as met. A voice failure never blocks a text line. A moderator's
  kick (recognised from the audit log) sets a voice hold: Amir leaves voice and joins no more
  until `/amir voice reconnect` lifts it, while the voice-log lines go on. A drag pins him to its
  channel (in memory) until nobody is left there; a move into AFK pins nothing. Any other
  disconnect, he follows the crowd again, quietly.

## The publishing gate

**Every channel write goes through one `GatedWriter`.** That covers outbox posts, cycle edits,
voice-log lines, and `/amir news test` once publishing is on. `build_services` creates the only
`DiscordSender` inside that gate.

With `publishing.enabled = false`, the gate does not even keep a reference to the sender, so no
code path can reach Discord channels. Instead:

- An outbox row is marked `previewed`. That status is terminal, so enabling publishing later
  never replays it.
- The would-be post is stored in the `previews` table, which `scripts/render_previews.py
  --from-db` can render.
- Voice-log lines are dropped and logged without any identity.

The switch is read once at startup. Turning it on is a config change plus a deploy, which
recreates the container (`--force-recreate`).

## Module map (`src/amir/`)

| Package | Responsibility |
|---|---|
| `__main__.py`, `health.py` | Entry point, `--check-config`, signal handling; Docker heartbeat check |
| `config/` | Pydantic settings from TOML; the token only from `DISCORD_TOKEN` |
| `logging/` | JSON lines to stdout, `America/New_York` timestamps, token redaction |
| `storage/` | SQLite (WAL), versioned migrations, repositories (parameterised SQL only) |
| `net/` | `HttpFetcher`: timeouts, retries, ETag/Last-Modified, body-size cap, User-Agent |
| `publishing/` | `Card` and Discord limits, sanitising, `GatedWriter`, `DiscordSender`, `OutboxWorker` |
| `news/` | Forum RSS and Steam parsers, URL canonicalisation, title filter, cards, `NewsPipeline` |
| `worldstate/` | Official and WFCD parsers, `WorldStatePoller` (cycles, Baro, item-name cache) |
| `baro/` | Item-name resolution, milestone schedule with catch-up, cards, `BaroService` |
| `cycles/` | Cycle roll-forward model, cards, `CycleService` (three living cards) |
| `voice/` | `classify`, `follow` (the census and the choice of channel), `stay` (one greeting per stay) and the rejoin window, clip library (FFmpeg-validated), queue, session, adapter (census reader, connector, player, kick detector), announcer, companion |
| `bot/` | `AmirBot` lifecycle, `TaskSupervisor` (one task per name, restart with backoff), `/amir` commands, reports |
| `persona.py` | Amir's wording for every human-facing line |

**Lifecycle.** `setup_hook` reads back any voice hold, then syncs the guild's commands (a failed
sync is logged, not fatal). The first `on_ready` settles interrupted sends, scans the clips,
starts each worker once and joins the biggest group quietly if people are already in voice and
no hold is in place; a gateway reconnect changes nothing. `close()` stops voice-follow and the
hold-save retry and then the workers, removes the heartbeat, leaves voice (at most 10 s), then
closes the gateway, HTTP and the database.

**Privacy.** Voice data is never written to SQLite (the voice hold is a time and a reason, with
no member data). The census, the stays and the pin live in memory only. Voice log lines carry no
member id or name, the companion's own log lines name channels only, `/amir` replies count
people per channel without naming them, and discord.py is never logged below INFO.
