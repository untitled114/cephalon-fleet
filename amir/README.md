# Amir Beckett

A Discord bot for one small Warframe server. Amir is a composed, dry-witted digital butler, not a
generic notifier. He sits in voice with the biggest group and greets people with pre-recorded
clips, logs arrivals, departures and moves in every voice channel, and posts official Warframe
news, Baro Ki'Teer's visits and live open-world cycle cards.

> **Publishing is OFF by default.** Until the owner approves the card designs, Amir sends, edits
> and deletes nothing in any Discord channel. Every would-be post is recorded as a preview
> instead. See [Turning publishing on](#turning-publishing-on).

- Version 0.1.0 ([CHANGELOG](CHANGELOG.md)).
- How it is built, and why: [`docs/architecture-and-reasoning.md`](docs/architecture-and-reasoning.md).

## Contents

1. [Features](#features)
2. [Architecture](#architecture)
3. [Runtime and dependencies](#runtime-and-dependencies)
4. [Local development](#local-development)
5. [Discord application setup](#discord-application-setup)
6. [Permissions and intents](#permissions-and-intents)
7. [Environment variables](#environment-variables)
8. [Configuration file](#configuration-file)
9. [Voice greetings](#voice-greetings)
10. [News sources](#news-sources)
11. [Slash commands](#slash-commands)
12. [Tests](#tests)
13. [Deployment (Docker on the VPS)](#deployment-docker-on-the-vps)
14. [Turning publishing on](#turning-publishing-on)
15. [Health checks](#health-checks)
16. [Logging and troubleshooting](#logging-and-troubleshooting)
17. [Backup, recovery and rollback](#backup-recovery-and-rollback)
18. [Updating dependencies](#updating-dependencies)
19. [Known limitations and future extensions](#known-limitations-and-future-extensions)

## Features

### Voice companion

- **Amir sits with the biggest group.** He is in voice only while people are. A Discord
  bot can be in one voice channel per server, so he sits in the one with the most people.
  - When the first person joins a voice channel he can sit in, he joins them at once.
  - When another channel gets more people than his own, and his own still has people in it, he
    waits a settle time, 30 s by default (`follow_settle_seconds`). He moves only if that channel
    still has more people then. One person peeking into another channel never moves him, and he
    never bounces back and forth. If his own channel empties while another has people, he goes
    to them at once.
  - A tie keeps him where he is. Out of voice, a tie goes to the home channel (Warframe,
    `voice_channel_id`) if it is among them, else to the tied channel highest in the server's
    channel list.
  - When nobody is left in any channel he can sit in, he leaves 60 s later.
  - He moves by leaving one channel and joining the next, never with a channel move, so he stays
    self-deafened. His own moves are his decision: never logged, never taken for a drag.
- **The channels he sits in:** regular voice channels where his role can view, connect and speak.
  Never the server's AFK channel ("someone parked in AFK isn't really there"), a stage channel, a
  channel with a user limit (a three-seat channel, say: he would take a seat a person needs),
  or a channel listed in `exclude_channel_ids`. Their people are still logged; they just don't
  count toward where he sits.
- **One greeting per stay in voice.** A stay runs from joining any voice channel to leaving voice
  altogether. He greets each person with a random clip from `assets/greetings/` the first time
  they share a channel during their stay: when they come into his channel, or when he comes into
  theirs within 30 s of them joining voice. Hopping between channels never earns a second
  greeting, whoever moves, and people already in a channel when he moves in are not greeted. When
  there is more than one clip, he never plays the same one twice in a row.
- Greetings never overlap and never cut each other off. They wait in a small queue, and a
  greeting that is more than 30 s late is dropped rather than played.
- Someone who leaves voice and comes back within 2 minutes is announced but not greeted again.
- **Every voice channel is logged.** Arrivals, departures and moves are posted as plain text in a
  dedicated voice-log channel, for every voice channel Amir can see, AFK included. A move between
  two channels is one line ("@Tenno has moved from #Warframe to #Arcade."), never a leave
  plus a join; it can be switched off with `announce_moves`. Discord's own idle move to AFK is
  logged as a move. A channel hidden from Amir's role never appears: a move into it is logged as
  a departure, and a move out of it as an arrival. Mentions never ping, and Amir never announces
  himself, his own moves, or other bots.
- After a restart with people already in voice, Amir joins the biggest group quietly: no
  greetings and no announcements. When he drops out of voice by accident (network, voice server),
  he follows the crowd again, quietly.
- **A drag pins him.** A drag from one channel to another is deliberate. After a drag he stays in
  that channel, even beside a bigger group, until nobody is left in it; then he follows the
  crowd again. That holds for any channel but AFK, even one he would never choose himself, such
  as a capped one; he greets nobody where he can't speak. A drag into an empty channel pins
  nothing, and nobody already there is greeted. The pin lives in memory, so a restart forgets it.
  `/amir voice reconnect` ends it too.
- **AFK is never a place he stays.** Whoever moves him there, a moderator or Discord's idle timer,
  he leaves AFK and follows the crowd. It is not a hold: to keep him out of voice, kick him.
- **A moderator's kick keeps him out.** If a moderator disconnects
  Amir from voice, from any channel, he stays out until an admin runs `/amir voice reconnect`,
  even across restarts. Meanwhile arrivals, departures and moves are still posted to the voice
  log, but nobody is greeted. A kick is recognised from the server's audit log, so Amir needs
  **View Audit Log**. A hold stored back when a drag still held him out (reason `moved`) also
  holds until then.
- He joins self-deafened. Voice data is never recorded or stored, and who was in which channel is
  kept in memory only.

### Warframe intelligence

- **Patch notes and hotfixes** from the official forum's PC Update Notes RSS feed. The card keeps
  the post's structure: bold headings, `•` bullets with `◦` sub-bullets, and short paragraphs, as
  many whole lines as fit in 900 characters, then a closing `…` if more follows. Images and
  italic image descriptions are left out.
- **Official announcements** from Digital Extremes' Steam news. These pass through a keyword
  exclusion filter, so Baro posts, soundtracks, TennoGen, bundles, merch and plush are left out.
- **Baro Ki'Teer** gets three separate cards per visit: 72 h before he arrives, on arrival,
  and 10 h before he leaves. The arrival card names his relay and lists his mods, one line each
  with ducat and credit prices. The rest of his stock is summed up in one line of category
  counts, e.g. "Also aboard: Skins and armour 28 · Ship decorations 7 · Other 4 · Glyphs 3 ·
  Weapons 3". A visit without mods says "No mods this visit." above that line.
- **Three living cycle cards** in their own channel: Plains of Eidolon (day/night), Orb Vallis
  (warm/cold) and Cambion Drift (Fass/Vome). Each is posted once and then edited in place, with
  a live countdown.
- **First start posts nothing.** Everything already in a feed is recorded as seen. After that,
  dedup lives in SQLite, so restarts never re-post.

### Operations

- Admin-only `/amir` slash commands, scoped to the one server.
- Docker Compose with a non-root user, a read-only root filesystem, a health check and capped
  logs.
- A deploy script that refuses to turn publishing on by accident.

## Architecture

There is one process: a discord.py client plus supervised background tasks. The core holds every
decision and has no Discord imports. Thin adapters connect it to Discord, HTTP, SQLite and FFmpeg.

All channel writes go through a single **`GatedWriter`** (the publishing gate). News and Baro
posts are delivered through a durable SQLite **outbox**.

See **[docs/architecture.md](docs/architecture.md)** for the data flow and the module map, and
**[docs/architecture-and-reasoning.md](docs/architecture-and-reasoning.md)** for why it is built
this way.

| Package (`src/amir/`) | Responsibility |
|---|---|
| `config/` | Validated settings (TOML file, plus the token from the environment) |
| `logging/` | JSON logs in America/New_York time, secret redaction |
| `storage/` | SQLite schema, migrations, repositories |
| `net/` | HTTP fetcher: timeouts, retries, conditional requests, size limits |
| `publishing/` | Cards and Discord limits, sanitising, the gate, the sender, the outbox |
| `news/` | Forum RSS and Steam parsers, filter, cards, polling pipeline |
| `worldstate/`, `baro/`, `cycles/` | World-state polling, Baro milestones and cards, cycle cards |
| `voice/` | Event classification, voice census and channel choice, stays and rejoin window, clip library, queue, voice session, announcer, companion |
| `bot/` | Client lifecycle, task supervisor, `/amir` commands, status reports |

## Runtime and dependencies

- **Python 3.12**: `requires-python = ">=3.12,<3.13"`, and the image uses
  `python:3.12-slim-bookworm`.
- **[uv](https://docs.astral.sh/uv/)** manages the environment. Versions are locked in `uv.lock`,
  and the image installs from it with `--frozen`.
- **FFmpeg** is required at run time: it validates and plays the clips. The image brings Debian's
  FFmpeg 5.1 and `libopus0`. Running the bot outside Docker needs `ffmpeg` on `PATH`.

Runtime packages (see `pyproject.toml`):

| Package | Why |
|---|---|
| `discord.py[voice]` 2.7.x | A well-maintained Discord library with voice support. The `voice` extra brings `PyNaCl` and `davey`, the DAVE end-to-end encryption that Discord voice now requires. |
| `aiohttp` | Async HTTP for the feeds. discord.py already depends on it. |
| `aiosqlite` | SQLite without blocking the event loop. No database server is needed. |
| `feedparser` | Tolerant RSS parsing. |
| `pydantic` v2 | Config validation with clear, value-free error messages. |
| `tzdata` | Time-zone data, because slim images ship none. |

Development tools: `pytest`, `pytest-asyncio`, `pytest-cov`, `imageio-ffmpeg` (an FFmpeg binary
for the tests when the system has none), `black`, `isort` and `ruff`.

Every runtime dependency in the lock has a prebuilt wheel for the server's platform or is pure
Python, so the server build compiles nothing.

## Local development

```bash
uv sync                                    # Python 3.12 venv with dev tools
cp config/amir.example.toml config/amir.toml
```

Edit `config/amir.toml` with your server's IDs. Keep `enabled = false`.

```bash
uv run python -m amir --check-config       # validate; prints a redacted report
```

`config/amir.toml` is git-ignored. It is the owner's real config, and `scripts/deploy.sh` copies
it to the server.

**Running the bot locally** connects to Discord for real. Do that only against a test server,
with publishing off:

```bash
export DISCORD_TOKEN=...                   # never commit it; never put it in amir.toml
uv run python -m amir                      # Ctrl+C shuts down cleanly
```

The environment is the only place the app reads the token from. It does not load `.env` itself.
To use a local `.env`, run `set -a; . ./.env; set +a` first.

**Previews** let you see the cards without Discord:

```bash
uv run python scripts/render_previews.py              # real captured data -> docs/previews/cards.html
uv run python scripts/render_previews.py --from-db amir-copy.sqlite3   # what a gated run would have posted
```

The preview page is self-contained HTML. `docs/previews/*.html` is git-ignored.

**Capturing fresh fixtures** (read-only HTTPS GETs):

```bash
uv run python scripts/capture_fixtures.py --only all
```

## Discord application setup

The application already exists: **Amir Beckett** (its application ID is on the Developer
Portal's **General Information** page). To set one up from scratch:

1. Open the [Discord Developer Portal](https://discord.com/developers/applications) and choose
   **New Application**.
2. On the **Bot** page:
   - Choose **Reset Token** and copy the token once, straight into the server's `.env` (see
     [Deployment](#deployment-docker-on-the-vps)). Never paste it in chat or commit it.
   - Under **Privileged Gateway Intents**, leave all three off: Presence, Server Members and
     Message Content. Amir needs none of them.
   - Optional: turn **Public Bot** off, so only you can add him to servers.
3. Optional: on the **Installation** page, turn off User Install. Without the bot scope and
   permissions, the default install link isn't useful. Use the invite URL below instead.
4. Invite the bot with this URL. It asks for the `bot` and `applications.commands` scopes and the
   seven permissions listed under [Permissions and intents](#permissions-and-intents):

   `https://discord.com/oauth2/authorize?client_id=<application ID>&scope=bot+applications.commands&permissions=3230848&integration_type=0`

5. Get the IDs. In Discord, open **User Settings → Advanced** and turn on **Developer Mode**.
   Then right-click each of these and choose **Copy ID**:
   - the server (`guild_id`)
   - the **Warframe** voice channel (`voice_channel_id`), his home channel: when he is out of
     voice and two channels tie for the most people, he picks it
   - the **news** text channel (`news_channel_id`)
   - the **cycles** text channel (`cycles_channel_id`)
   - the **voice log** text channel (`voice_log_channel_id`)

   Put all five in `config/amir.toml`.
6. If any of those channels is private, give Amir's role access to it, with the permissions
   below.

## Permissions and intents

**Gateway intents:** `guilds` and `voice_states` only. Both are non-privileged. Voice-state
events carry the member, so Amir needs no Members, Message Content or Presence intent.

**Permissions:** the invite URL's integer is `3230848`, the sum of the seven below.

| Permission | Bit | Used for |
|---|---|---|
| View Audit Log | 128 | Telling a moderator's kick from an accidental disconnect (the audit log's member-disconnect entries; Amir never reads anything else in it) |
| View Channels | 1024 | Seeing the configured channels, and the voice channels he logs and may join |
| Send Messages | 2048 | News, Baro, cycle and voice-log posts (only while publishing is on) |
| Embed Links | 16384 | The cards are embeds |
| Read Message History | 65536 | Finding his own post among recent messages after a crash mid-send |
| Connect | 1048576 | Joining a voice channel |
| Speak | 2097152 | Playing the greeting |

Amir edits only his own messages, so he needs no Manage Messages.

**Voice channels.** For Amir to follow the group into a voice channel, his role needs View
Channel, Connect and Speak there; a channel lacking any of them is never one he chooses. A voice
channel hidden from his role is no channel to him: it is never joined, logged or counted, even
though Discord may still send him its voice updates.

**Existing installs: grant View Audit Log by hand.** Amir was first invited without it. In
**Server Settings → Roles**, open Amir's role and turn on **View Audit Log**.
Until then he logs `can't read the audit log; grant View Audit Log so a moderator's kick keeps
Amir out` once, and treats every disconnect as an accident: he rejoins even after a kick.

The optional role ping (`notification_role_id`, off by default) also needs that role to be
mentionable. Otherwise grant the role-mention permission yourself. Amir's own mentions never
ping: every send uses `AllowedMentions.none()`, except that one configured role.

## Environment variables

| Variable | Required | Meaning |
|---|---|---|
| `DISCORD_TOKEN` | yes | The bot token. Read only from the environment. On the server it lives in `.env` in the deploy folder (see `.env.example`). It is redacted from every log line. |
| `AMIR_CONFIG` | no | Path to the TOML config. Default `config/amir.toml`; in the image, `/app/config/amir.toml`. |
| `DEPLOY_SERVER` | for deploys | Used only by `scripts/deploy.sh`: the server's login, `user@host`. Set it here or as `DEPLOY_SERVER=user@host` in `.env.deploy` (git-ignored); there is no default. |
| `AMIR_DEPLOY_DIR` | for deploys | Used only by `scripts/deploy.sh`: the server folder it syncs into and builds in. Set it here or as `AMIR_DEPLOY_DIR=<path>` in `scripts/.deploy.env` (git-ignored); there is no default, and a deploy without it stops. |

## Configuration file

`config/amir.example.toml` is the complete, commented reference. Most keys have a default,
shown below. Two kinds of key must be set:

- the five Discord IDs (the bot refuses to start, with exit code 2, while any is still 0);
- every field of each `[[news.sources]]` entry except `enabled`. With no entry at all, no news
  is polled.

Unknown keys are rejected. `python -m amir --check-config` validates the file without
connecting to anything and never prints the token or a path.

| Section | Keys (defaults) |
|---|---|
| `[discord]` | `guild_id`, `voice_channel_id`, `news_channel_id`, `cycles_channel_id`, `voice_log_channel_id` (all required); `notification_role_id = 0` (no pings) |
| `[publishing]` | `enabled = false`. **The hard switch.** Only the owner turns it on. |
| `[voice]` | `greetings_dir = "assets/greetings"`, `ffmpeg_path = "ffmpeg"`, `audio_filter = "loudnorm=I=-16:TP=-1.5:LRA=11"`, `leave_grace_seconds = 60`, `greeting_expiry_seconds = 30`, `queue_capacity = 5`, `rejoin_window_seconds = 120`, `greet_on_rejoin = false`, `announce_joins = true`, `announce_leaves = true`, `announce_moves = true`, `connect_timeout_seconds = 15`, `reconnect_max_attempts = 5`, `exclude_channel_ids = []`, `follow_settle_seconds = 30` |
| `[news]` | `exclude_title_keywords = ["baro ki'teer", "soundtrack", "single release", "tennogen", "battlekit", "bundle", "merch", "plush"]`, `role_ping_categories = []`, and `[[news.sources]]` (no default entries; see [News sources](#news-sources)) |
| `[worldstate]` | `official_url = "https://api.warframe.com/cdn/worldState.php"`, `wfcd_url = "https://api.warframestat.us/pc"`, `items_url = "https://api.warframestat.us/items/?only=uniqueName,name"`, `interval_seconds = 300`, `flip_refresh_delay_seconds = 5`, `items_refresh_hours = 24` |
| `[cycles]` | `worlds = ["cetus", "vallis", "cambion"]`, `durations = { day = 6000, night = 3000, warm = 400, cold = 1200, fass = 6000, vome = 3000 }` (seconds per phase) |
| `[baro]` | `warn_before_arrival_hours = 72`, `warn_before_departure_hours = 10` |
| `[http]` | `connect_timeout_seconds = 10`, `read_timeout_seconds = 30`, `max_retries = 3`, `max_response_bytes = 5000000`, `user_agent = "AmirBeckett/0.1 (+Discord bot)"` |
| `[storage]` | `db_path = "data/amir.sqlite3"` |
| `[logging]` | `level = "INFO"`, `heartbeat_path = "data/heartbeat"`, `voice_debug = false` |

**Voice channels Amir skips.** `exclude_channel_ids` lists voice channels Amir never chooses to sit in; he still logs them. `follow_settle_seconds` is how long a bigger group elsewhere must keep its lead before he moves.

**Data paths in the container.** The image links `/app/data` to the `/data` volume. So the
example's `data/amir.sqlite3` and `data/heartbeat` are `/data/amir.sqlite3` and
`/data/heartbeat`, and writing the absolute form gives the same files. Never point them
anywhere else: the container's root filesystem is read-only, and only `/data` survives a
rebuild.

## Voice greetings

Clips live in `assets/greetings/`. Its [README](assets/greetings/README.md) is the asset
manifest: naming, formats and rules.

- **Naming:** lowercase kebab-case, numbered, such as `amir-greeting-01.qt` or
  `amir-greeting-02.mp3`.
- **Formats:** `.mp3`, `.wav`, `.ogg`, `.opus`, `.m4a`, `.mp4`, `.aac`, `.flac`, `.qt`, `.mov`,
  meaning anything FFmpeg decodes that has one of those extensions. Phone recordings (`.qt`)
  work as they are.
- **Validation:** every clip is fully decoded with FFmpeg at startup. A clip that fails is
  logged by file name and never played. With no valid clip, Amir still joins and announces,
  silently, and logs a warning.
- **Loudness:** every clip plays through FFmpeg's `loudnorm` filter (about -16 LUFS, peaks at or
  below -1.5 dBTP). The files themselves are never changed.
- **Queue rules:**
  - One greeting plays at a time, and the queue holds at most 5.
  - A member already waiting is not queued twice.
  - When the queue is full, the newest arrival's greeting is dropped (logged). Their text line
    is still posted.
  - A greeting that has waited more than 30 s is skipped.
- **Adding a clip:** put the file in `assets/greetings/` and deploy. On the server the folder is
  mounted read-only into the container. `/amir voice greetings` re-checks the clips without a
  restart.
- **What belongs there:** only audio the owner is entitled to use.

## News sources

Each `[[news.sources]]` entry has these fields:

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | Stable identifier. Dedup and baseline state are keyed by it, so don't rename it. |
| `kind` | yes | `forum_rss` or `steam_news` (the two adapters in 0.1) |
| `label` | yes | Shown in `/amir news sources` |
| `url` | yes | The feed URL |
| `interval_seconds` | yes | Poll interval in seconds. There is no default; the example uses 300 for the forum and 900 for Steam. |
| `enabled` | no (default `true`) | `false` stops polling, but the source's state is kept |

The example config enables two verified official sources:

- **Official forums, PC Update Notes:**
  `https://forums.warframe.com/forum/3-pc-update-notes.xml/`. The slug is required; bare-ID URLs
  don't work. Every new topic is posted.
- **Official Steam announcements:** Steam's `GetNewsForApp` for app 230410 with
  `feeds=steam_community_announcements`. Without that filter, third-party press shows up.
  `exclude_title_keywords` applies to Steam items only. Matching is case-insensitive, and every
  skipped item is logged with the keyword that excluded it.

**Baseline.** On a source's first successful poll, Amir records every item in it as seen and
posts nothing. That applies to a newly added source too. After the baseline, a never-seen item
dated more than 7 days back is recorded rather than posted. This matters because the forum feed
is ordered by activity, so a reply can bring an old topic back.

**Dedup.** Items are deduplicated by stable ID (forum `guid`, Steam `gid`), or by canonical URL
(tracking parameters removed) when there is none. Edits to an item already posted are not
re-posted. A forum patch note and a Steam post about the same update are different items, and
both are posted.

**Baro and cycles** come from the official world state (`api.warframe.com`) and from WFCD
(`api.warframestat.us`). WFCD supplies the cycles, the relay names and the item-name database.
The item names are cached in SQLite and refreshed at most once a day.

**Role pings** are off. To ping a role for some categories, set `notification_role_id` and list
the categories in `role_ping_categories`. The categories are `"Update"`, `"Hotfix"` (forum),
`"Announcement"` (Steam) and `"Baro Ki'Teer"`.

**Adding a community source** later takes a new `kind` adapter plus configuration. None ships in
0.1.

## Slash commands

All commands sit under `/amir`. Only administrators can use them, and only in the configured
server. Discord hides them from everyone else, and every command checks again. Every reply is
ephemeral (only the asking admin sees it).

| Command | What it does |
|---|---|
| `/amir status` | Brief overview: publishing state, voice, sources, outbox, next Baro and cycle change |
| `/amir health` | How each part is running, including failing news sources and cycle cards, with no secrets |
| `/amir version` | The installed version |
| `/amir voice status` | Which voice channel Amir is in and why: "in #Arcade, with the most people", "pinned in #Warframe by a drag since ...", or held out by a kick. Then how many people are in each voice channel, counts only: "People in voice: #Warframe 3 · #Arcade 1". `/amir status` and `/amir health` show the same voice line. |
| `/amir voice reconnect` | Lift a moderator's kick hold and end a drag's pin, then let Amir follow the crowd. Out of voice, he joins the busiest channel at once, or with the next arrival if nobody is in voice. Still in a channel with people (after a pin, say), he moves to a bigger group only after the 30 s settle time, like any other time. The reply names the channel he is in meanwhile. The command never makes him leave and rejoin on its own. Run within about 2 s of a kick, the kick check still under way wins: he stays out, and the reply shows the hold. |
| `/amir voice test` | Queue a test greeting in whichever channel Amir is in; only while he is in voice with people, and where his role may speak (pinned in a channel where it may not, the reply says so) |
| `/amir voice greetings` | Re-check the clips and report how many are valid |
| `/amir news status` | Each source's last poll and success, plus the outbox counts |
| `/amir news check` | Poll every source now. Dedup still applies, so nothing is posted twice. |
| `/amir news sources` | The configured sources |
| `/amir news test` | Publishing off: shows a test card in the reply only. Publishing on: posts a card labelled "Test: not a real announcement" to the news channel. |
| `/amir config validate` | The redacted config report |
| `/amir cycles refresh` | Bring the cycle cards up to date now, and report each world's state and any card failing to update |
| `/amir baro next` | Baro's next milestone times |

**Registration.** The commands are registered to the configured server only, at every startup,
so changes show up immediately (there is no global propagation delay). If the sync fails, for
example because the bot was invited without `applications.commands`, Amir logs it and keeps
running. Re-invite him with the URL above.

**Testing on a development server.** Use a separate config whose `guild_id` and channel IDs
point at a private test server. Keep publishing off unless that server is the one you want posts
in.

## Tests

```bash
uv run pytest                     # full suite with the 96% coverage gate
uv run pytest -q --no-cov tests/news           # a subset (the gate is for full runs only)
uv run ruff check . && uv run ruff format --check .
```

- The tests never touch Discord or the network. Discord objects are faked.
- Feed and world-state inputs are **real captures** in `tests/fixtures/captured/`. Edge cases
  are derived from copies at test time, never hand-written.
- Audio tests play a real recording: NASA's public-domain Apollo 11 clip in `tests/fixtures/audio/`.
- FFmpeg comes from `PATH`, or else from `imageio-ffmpeg`.
- `tests/test_packaging.py` runs the deploy script with fake `ssh` and `rsync`, so nothing leaves
  the machine.

## Deployment (Docker on the VPS)

Amir runs as a container on a server reached over SSH (its login in `DEPLOY_SERVER`, its folder in
`AMIR_DEPLOY_DIR`). The image is **built on the server**, where it runs: `scripts/deploy.sh`
does that.

### Server prerequisites

- **Docker Engine with the Compose v2 plugin.** On a fresh Debian or Ubuntu VPS, install Docker
  from Docker's official repository (<https://docs.docker.com/engine/install/>) with a
  sudo-capable account, then add the deploy user to the `docker` group.
- **SSH access and `rsync`** on both machines.
- **Nothing else.** The image brings its own Python and FFmpeg. The server's own Python and
  missing FFmpeg don't matter.

### First deployment

1. **Put the token on the server.** Do this once, by hand, over SSH.

   ```bash
   ssh "$DEPLOY_SERVER"
   mkdir -p <the folder in AMIR_DEPLOY_DIR> && cd <that folder>
   nano .env                 # one line: DISCORD_TOKEN=<the token>
   chmod 600 .env
   ```

2. **Locally,** create `config/amir.toml` with the five IDs (see [Local
   development](#local-development)), then check it.

   ```bash
   uv run python -m amir --check-config
   ```

3. **Deploy.** Run the dry run first.

   ```bash
   scripts/deploy.sh --dry-run    # validates the config; connects to compare, copies nothing, runs no docker command
   scripts/deploy.sh              # rsync, then on the server: docker compose build, up -d --force-recreate
   ```

What `scripts/deploy.sh` does:

- **It refuses to run** in any of these cases:
  - `config/amir.toml` is missing or invalid. The check uses Amir's own config loader.
  - Any of the five Discord IDs is still 0.
  - The project's `.venv` is missing. Run `uv sync`.
  - The config turns publishing on and `--allow-publishing` wasn't given.
- **It rsyncs** the repo to the server folder set in `AMIR_DEPLOY_DIR` with `--delete`.
  - It excludes `.git`, `.venv`, `data`, every `.env*` file except `.env.example`,
    `docs/previews` and local caches. Excluded paths on the server, including its `.env`, are
    never overwritten or deleted.
  - It makes every synced file world-readable (`--chmod=Do+rx,Fo+r`), so the container's UID
    10001 can read the config and the clips whatever their local mode. None of these files is
    secret.
  - `--dry-run` still connects to the server to compare, but copies nothing and runs no docker
    command.
- **On the server, it rebuilds and starts a new container.** In that folder it runs
  `docker compose build`. If that produced a different image, the image that was running is
  tagged `amir-beckett:previous` (for a rollback); a rebuild that changes nothing leaves that
  tag where it was. Then it runs `docker compose up -d --force-recreate`.

`--force-recreate` is what makes a config-only change take effect. `config/amir.toml` is a
single-file bind mount, and rsync replaces the file rather than editing it, so a running
container keeps reading the old copy. A change to the config alone leaves the image and
`compose.yaml` the same, so without the flag compose would keep that container, and the old
setting would stay live until some unrelated restart. So **change the config locally and
deploy**. Don't edit `config/amir.toml` on the server by hand: the next deploy overwrites it.

Keep server-side files you care about, such as backups, **outside** the deploy folder. The
`--delete` flag removes anything there that doesn't exist locally.

### What the container looks like

| Setting | Value |
|---|---|
| User | `amir`, UID 10001 (non-root) |
| Root filesystem | read-only, with `tmpfs` at `/tmp` |
| Capabilities | `cap_drop: [ALL]`, plus `no-new-privileges` |
| Process | `init: true`, so tini forwards SIGTERM. Amir leaves voice (at most 10 s), closes the gateway, then the database. `stop_grace_period: 20s`. |
| Restart | `restart: unless-stopped`: back after crashes and reboots |
| Volumes | `amir-data:/data` (SQLite and heartbeat); `./config/amir.toml` and `./assets/greetings` mounted read-only |
| Logs | Docker `json-file` log driver, 3 files of 10 MB each |
| Secrets | None in the image or the repo. The token comes from `.env` via `env_file`. |

### Everyday commands (on the server, in the deploy folder)

```bash
docker compose ps                         # state and health
docker compose logs -f --tail=100         # follow the logs
docker compose stop                       # graceful stop (SIGTERM, 20 s grace)
docker compose up -d                      # start again
docker compose restart                    # restart
docker compose run --rm --no-deps amir python -m amir --check-config    # validate the mounted config
docker compose down                       # remove the container; the amir-data volume (database) stays
docker image prune -f                     # delete dangling images left by rebuilds
```

`docker image prune -f` deletes only untagged images. `amir-beckett:previous` keeps its tag,
so the rollback image survives. Each deploy that builds a new image moves that tag to the image
that was running, and the image it replaces becomes dangling.

**Never run `docker compose down -v`.** It deletes the database volume.

To update, run `scripts/deploy.sh` again from your machine.

### Firewall and network

- **Inbound:** nothing. Amir opens no ports.
- **Outbound HTTPS and secure WebSockets (TCP 443)** to:
  - Discord's API, `https://discord.com/api`, and its gateway. Discord's docs give
    `wss://gateway.discord.gg/?v=10&encoding=json` as an example gateway URL. The real one
    comes from the Get Gateway Bot endpoint.
  - The four data hosts: `forums.warframe.com`, `api.steampowered.com`, `api.warframe.com` and
    `api.warframestat.us`.
- **Discord voice:**
  - A WebSocket to the voice endpoint Discord hands out for each connection. The docs' example
    is `sweetwater-12345.discord.media:2048`, so the port is not necessarily 443.
  - Then UDP to the voice server IP and port given in that handshake. The docs say "your client
    must be able to receive UDP packets, even through a firewall or NAT".

  So allow outbound TCP and UDP to Discord's voice servers on any port; a stateful firewall lets
  the replies back in. A cloud server's security-group or firewall egress rules must allow this;
  an "all egress" default does.
- **Sources** (Discord's developer documentation, accessed 2026-10-26):
  <https://docs.discord.com/developers/reference> (base URL),
  <https://docs.discord.com/developers/events/gateway> (gateway URL) and
  <https://docs.discord.com/developers/topics/voice-connections> (voice endpoint and UDP).
- **At build time only:**
  - Docker Hub (`python:3.12-slim-bookworm`)
  - `ghcr.io` (the `uv` image)
  - `deb.debian.org` (FFmpeg)
  - PyPI (`pypi.org` and `files.pythonhosted.org`)

## Turning publishing on

Publishing stays off until the owner approves the card designs. Turning it on is a deliberate,
recorded step:

1. **Render the previews** from real data: `uv run python scripts/render_previews.py`. That
   writes `docs/previews/cards.html`. To see what a gated server run would have posted, back up
   the server's database (see [Backup](#backup)) and render the copy with
   `--from-db <copy>`.
2. **The owner reviews and approves** the cards. Any design changes are made and re-previewed
   first.
3. **Locally,** set this in `config/amir.toml`:

   ```toml
   [publishing]
   enabled = true
   ```

4. **Deploy with the explicit flag.** Without it, the script refuses. The deploy recreates the
   container (`--force-recreate`), and that is what makes the new setting live (see
   [First deployment](#first-deployment)). Don't rely on editing the file on the server.

   ```bash
   scripts/deploy.sh --dry-run --allow-publishing
   scripts/deploy.sh --allow-publishing
   ```

5. **Check that it took.** In `docker compose logs`, the newest startup line must read
   `Amir Beckett 0.1.0 starting; publishing ON`, and `/amir status` must show `Publishing: ON`.

What happens when publishing goes on:

- Nothing previewed while publishing was off is replayed.
- Each of the three cycle cards is posted exactly once, then edited in place.
- News and Baro post from then on.

**To turn publishing off again:**

1. Set `enabled = false` locally.
2. Run `scripts/deploy.sh`. No flag is needed, and it recreates the container.
3. Confirm the newest startup line reads `publishing OFF`, and that `/amir status` shows
   `Publishing: OFF (previews only)`.

The switch is read only when the container starts. Until a new container is running, the old
setting stays in force.

## Health checks

- **How it works:**
  - While the Discord client is ready, Amir writes a heartbeat file (`/data/heartbeat`) every
    30 s.
  - Docker runs `python -m amir.health` every 30 s. That exits 0 if the heartbeat is at most
    120 s old.
  - After 3 failures in a row, Docker marks the container **unhealthy**. A 60 s start period
    covers the time before Discord's READY.
  - On shutdown the heartbeat file is removed, so a stopping bot reports unhealthy at once.
- **What healthy means:** the process and its event loop are running, and its Discord client is
  up. It does **not** mean every worker is healthy. A failing feed, a voice problem or a stuck
  outbox row does not make the container unhealthy. Use `/amir health`, `/amir status` and
  `/amir news status` for those.
- **What Docker does with it:** it only *reports* health, and it does not restart an unhealthy
  container. `restart: unless-stopped` covers crashes and reboots. To check health:

  ```bash
  docker inspect --format '{{.State.Health.Status}}' "$(docker compose ps -q amir)"
  ```

## Logging and troubleshooting

### Log format

Logs go to stdout as **JSON lines**. Each line has `ts` (ISO-8601, America/New_York), `level`,
`logger` and `event`. Docker keeps them, rotating at 3 × 10 MB.

```bash
docker compose logs --tail=200 amir
docker compose logs amir | grep '"level": "ERROR"'
```

- Set `[logging] level` to change verbosity. discord.py's own loggers never go below INFO,
  because its DEBUG output would include member data.
- Bot tokens and token-shaped strings are replaced with `[REDACTED]` everywhere.
- Voice events are logged without member IDs or names. Config errors name the problem and the
  file name, never a value or a full path.

### Startup lines to expect

- `Amir Beckett 0.1.0 starting; publishing OFF`
- `slash commands synced`, or the sync failure in [Common problems](#common-problems).
- `discord ready`
- `greetings scanned: 1 valid, 0 invalid` (counts only, no file names; a failing clip is also
  named in its own `greeting clip failed validation: <file>` warning).
- `Amir is joining voice channel <id>: the most people are there`, then `voice connected on
  attempt 1`, when someone is in a voice channel he can join. Each later move logs `Amir is
  joining voice channel <id>: ...` first, then `voice disconnected` as he leaves his channel,
  then `voice connected on attempt 1`.
- `Amir is held out of voice (kicked): not joining; /amir voice reconnect brings him back`, when
  a moderator's kick from an earlier run still holds him out (`moved` for a hold stored when a
  drag still held him out).
- With publishing off: `publishing disabled: send to news recorded as a preview`, whenever a
  post would have gone out.

### Common problems

| Symptom | Likely cause and fix |
|---|---|
| Container restarts, logs `config error: ...`, exit code 2 | Fix `config/amir.toml` (each line names the key) or `.env`, then redeploy. The five IDs must be set. |
| `Discord rejected the token in DISCORD_TOKEN` | The token was reset or mistyped. Update `.env` on the server, then `docker compose up -d`. |
| `slash command sync failed (HTTP 403); running without it` | The bot was invited without `applications.commands`. Re-invite with the URL above. |
| `Failed to connect to voice... Retrying in 1.0s...` with `WebSocket closed with 4006` | Discord's voice server rejected a brand-new voice session ("session no longer valid"); discord.py retries and connects about a second later, and nothing is missed. Amir pauses 0.5 s before a new session identifies, which should prevent it. If it keeps appearing, set `voice_debug = true` under `[logging]` and redeploy: discord.py then logs each voice-connection step (states and voice servers only, no member data). |
| No greeting is heard | Check `/amir voice greetings` and look for `greeting clip failed validation: <file>` or `no valid greeting clips found`. Check the Connect and Speak permissions on the channel he is in, and that outbound UDP is allowed. |
| `voice connect gave up after 5 attempts` | Voice is unreachable or not permitted. Text announcements keep working regardless. Amir retries at the next voice change, or use `/amir voice reconnect`. |
| Amir stays out of voice although people are in voice | A moderator kicked him, so he is held out: `/amir status` says `held out of voice since ... (kicked)`, or `(moved)` for a hold stored when a drag still held him out. Run `/amir voice reconnect` to bring him back. Otherwise everyone is in channels he never sits in: AFK, a stage, a capped or excluded channel, or one where his role lacks View Channel, Connect or Speak. |
| Amir stays in a smaller group | A drag pinned him: `/amir status` says `pinned in #... by a drag since ...`. He stays until nobody is left there. `/amir voice reconnect` ends the pin; while people are still with him, he then moves to the bigger group after the 30 s settle time, and the reply names the channel he is in meanwhile. Otherwise the bigger group is still in its 30 s settle time, or it is in a channel he never sits in. |
| `audit log unavailable (<error>): until it can be read again, ...` or `audit log unreachable (<error>): ...` | Discord answered the audit log read with an error, or didn't answer (network, timeout). Nothing to fix on your side; Amir reads it again at the next disconnect, join or refresh. Meanwhile a disconnect counts as accidental, and a stale snapshot can later take an accident for a kick. Each is logged once per process; every failed read is also logged at DEBUG as `reading the audit log failed: <error>`. |
| `saving the voice hold failed: <error>; it holds in memory, and saving is retried` | The database refused the write, so the hold is in force but not yet saved. Amir saves it again after 1 s, then 2, 4 ... at most every 60 s, one such line per failed attempt, until it is saved or an admin lifts it. Fix the `/data` volume (space, permissions); the next attempt then saves it, with nothing else to do. `/amir voice reconnect` lifts the hold at any time, saved or not, and the retry stops. If Amir restarts before it is saved, the hold is lost and he rejoins with the group. |
| `can't read the audit log; grant View Audit Log so a moderator's kick keeps Amir out` | Amir's role lacks View Audit Log, so he can't tell a kick from an accident and rejoins after every disconnect. Grant it (see [Permissions and intents](#permissions-and-intents)); the next disconnect is read correctly, no restart needed. |
| `news fetch failed source=...` | That source is down or slow. The others keep polling, and `/amir news status` shows the last success. |
| `outbox row ... failed after 8 attempts` | Discord kept refusing the post (permissions, or a deleted channel). Fix the channel; the row stays `failed` and is not retried automatically. |
| `docker compose ps` shows `unhealthy` | The Discord client hasn't been ready for a couple of minutes. Read the logs for gateway or token errors. |
| Nothing is posted anywhere | Expected while `publishing.enabled = false`. |

## Backup, recovery and rollback

### Backup

The database is `/data/amir.sqlite3` in the `amir-data` volume. The commands below take a
consistent online copy with SQLite's backup API (the same mechanism as the `sqlite3` CLI's
`.backup`) while the bot keeps running. Then they copy it out of the volume.

```bash
ssh "$DEPLOY_SERVER"
cd <the deploy folder> && mkdir -p ~/amir-backups
docker compose exec amir python -c "import sqlite3; s=sqlite3.connect('/data/amir.sqlite3'); d=sqlite3.connect('/data/backup.sqlite3'); s.backup(d); d.close(); s.close()"
docker compose cp amir:/data/backup.sqlite3 ~/amir-backups/amir-$(date +%F-%H%M).sqlite3
docker compose exec amir rm /data/backup.sqlite3
```

Keep the backups in `~/amir-backups/`, outside the deploy folder, because the deploy's
`--delete` would remove them otherwise. To copy one to your machine (for `render_previews.py
--from-db`, say), run `scp "$DEPLOY_SERVER":amir-backups/<file> .`.

### Restore

Stop the bot first. The backup API writes into the live database file safely, even if a
leftover WAL file is present.

```bash
cd <the deploy folder>
docker compose stop amir
docker compose run --rm --no-deps -v ~/amir-backups:/backup:ro amir python -c "import sqlite3; s=sqlite3.connect('file:/backup/<file>?immutable=1', uri=True); d=sqlite3.connect('/data/amir.sqlite3'); s.backup(d); d.close(); s.close()"
docker compose up -d
```

After a restore, dedup state is as of the backup. Items first seen after the backup was taken
may be posted again, unless they are more than 7 days old.

### Rollback

Each deploy that builds a new image keeps the one that was running as `amir-beckett:previous`.
A deploy that rebuilds the same image (a config-only change, say) leaves it alone, so it is
always an older build than `amir-beckett:latest`. To go back to it:

```bash
cd <the deploy folder>
docker image tag amir-beckett:previous amir-beckett:latest
docker compose up -d --no-build --force-recreate
```

That rolls back the code in the image. The synced files on the server are still the new ones,
so the next deploy rebuilds from whatever you sync. For a lasting rollback, check out the
earlier commit locally and run `scripts/deploy.sh`.

The database is not rolled back. The 0.1 schema is version 1. If a future release migrates
it, back up before upgrading.

## Updating dependencies

```bash
uv lock --upgrade-package discord-py      # one package (or: uv lock --upgrade for everything)
uv sync
uv run pytest && uv run ruff check . && uv run ruff format --check .
```

Commit `pyproject.toml` and `uv.lock`, then deploy. The image installs exactly what the lock
says (`uv sync --frozen`).

- **discord.py:** keep it current within `<2.8` unless you test a new minor. Discord voice
  changes (such as DAVE) arrive through discord.py and `davey` updates.
- **uv:** bump the version tag in the Dockerfile's `COPY --from=ghcr.io/astral-sh/uv:<version>`
  line.
- **Base image and Debian packages** (FFmpeg, libopus and security fixes) are refreshed only when
  the base image is re-pulled. Now and then, on the server, run:

  ```bash
  docker compose build --pull && docker compose up -d
  ```

## Known limitations and future extensions

### Limitations

- **Publishing is read at startup.** Changing it needs a deploy, which recreates the container.
- **Health means alive, not all-healthy** (see [Health checks](#health-checks)). Docker does not
  restart an unhealthy container.
- **Delivery is effectively once, not provably exactly once.** A post counts as sent only after
  Discord confirms it. If Amir crashes after Discord accepted a post but before he recorded it,
  he searches the channel's last 50 messages for it at the next start. If he can't find it (it
  was deleted, or has scrolled out of reach), he posts it again. After 8 failed attempts a post
  is marked `failed` and left alone.
- **A kick is recognised from the audit log.** Without View Audit Log every disconnect is
  treated as accidental, and Amir rejoins. Discord's member-disconnect entries don't say who was
  disconnected, and it folds a moderator's disconnects into one entry. So Amir compares the
  audit log with a snapshot he takes when he joins voice and again 3 s after anyone else leaves
  voice. Only a moderator disconnecting someone else in the few seconds around Amir dropping out
  by accident can still be taken for a kick: Amir then stays out until `/amir voice reconnect`.
  If the audit log can't be read for that refresh (a Discord outage), the window is wider until
  his next join. The reverse edge is narrower still: a snapshot read that Discord answers just
  after a kick, but that completes before the news of Amir's own disconnect reaches him (gateway
  latency, normally well under a second), takes in the kick, which then goes unseen; he rejoins,
  and a second kick holds him out. A read still under way when that news arrives is thrown away.
- **Cycle cards after publishing goes off and on again.** If publishing is turned off, a world
  changes state, and publishing is turned back on, that world's live card can show the old state
  until its next change.
- **WFCD data can lag.** Cycle snapshots from `api.warframestat.us` can be over an hour old.
  Amir ignores stale ones and rolls the cycle forward locally from the configured phase lengths,
  so a card can be off by a little until a fresh snapshot arrives.
- **The item-name database comes from WFCD** (about 1.8 MB, sometimes slow). If it has never
  loaded, or lacks a new item, that Baro item's name falls back to WFCD's Baro listing, or to a
  name made from the item's internal path. Names are cached in SQLite and refreshed at most daily.
- **Sources can change without notice.** The forum's HTML sits behind a Cloudflare challenge; its
  RSS feed does not today. The official world-state URL has moved before. A failing source is
  isolated and reported, never fatal.
- **Voice-log lines live in memory only.** Lines not yet posted are lost on restart. During a
  long Discord outage, at most 50 are kept, and the oldest are dropped, with a WARNING.
- **One server, one voice channel at a time.** A Discord bot can be in one voice channel per
  server, so Amir sits with the biggest group and greets only in his own channel. Everyone else
  is still logged.
- **A kick during his own move is missed once.** When Amir leaves a channel to follow the crowd,
  he expects that disconnect. A moderator's kick landing in that moment (the time Discord takes
  to confirm his leave, normally well under a second) is taken for his own leave, and he joins
  the new channel. The window opens on every follow move. Kicking him again holds him out.
- **A reconnect right after a kick is overridden.** `/amir voice reconnect` run within about 2 s
  of a kick, while Amir is still checking the audit log for it, is overridden when that check
  finds the kick: he stays out, and the reply shows the hold. Run the command again.
- **Discord's idle move ends a pin.** If Discord moves Amir to AFK for being idle, he leaves AFK
  and follows the crowd, like after any move to AFK, so a moderator's pin ends. Drag him again to
  pin him.
- **His channel is read from his voice connection.** While discord.py resumes that connection
  or moves it to another voice server, a decision made in that moment may misread which channel
  Amir is in, and he may drop out of voice and rejoin.
- **A greeting to a stage audience may play silently.** Dragged into a stage channel, Amir joins
  its audience, whom Discord keeps muted until they are invited to speak.
- **A pin ends when he can't get back in.** If Amir drops out of a pinned channel by accident and
  it refuses his rejoin (it is full, or it is a stage channel, which he never joins on his own),
  or his connects keep failing for about 105 s (5 attempts with the default timeout and backoff),
  the pin ends and he follows the crowd.
- **English only.** All of Amir's text is English. Unicode names and titles are kept intact,
  but nothing is translated.

### Future extensions

- Community news sources: a new `kind` adapter plus configuration.
- More open worlds (Earth, Zariman, Duviri): configuration plus a small mapping.
- Text-to-speech. See [docs/tts-evaluation.md](docs/tts-evaluation.md). It is not implemented,
  and news is never spoken.
- Fleet health monitoring (Atlas).
