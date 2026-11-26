# Amir Beckett: architecture and the reasoning behind it

- **Written:** 2026-10-27, for version 0.1.0 as deployed on the production server.
- **Companions:** [`architecture.md`](architecture.md) maps the modules and the data flow. This
  document explains **why** the system is built the way it
  is: the decision, the reason, what else was considered, and what it costs.

---

## 1. The shape in one paragraph

Amir is **one Python 3.12 process** in one Docker container. Inside it, a discord.py client and a
handful of supervised background tasks share one asyncio event loop. Pollers read the official
Warframe sources and the community world-state API. They turn what they find into **cards**, and
queue news and Baro posts in a **SQLite outbox**. A single **publishing gate** decides whether
anything actually reaches a Discord channel. The voice side listens to voice-state events in
every voice channel, writes arrival, departure and move lines through the same gate, sits with
the biggest group, and plays the owner's greeting clip through FFmpeg. Every decision (what is
news, when Baro is due, which cycle is live, who counts as arriving, which channel to sit in)
lives in a core with no Discord imports. Thin adapters connect that core to Discord, HTTP,
SQLite and FFmpeg.

```
 Forum RSS, Steam API ───▶ NewsPipeline ──┐
 Official world state ───▶ BaroService ───┼─▶ outbox (SQLite) ─▶ OutboxWorker ─┐
 WFCD world state ───────▶ CycleService ──┼──────── 3 living cards ────────────┤
 Voice-state events ─────▶ VoiceCompanion ┴─▶ voice-log lines (memory) ────────┤
                                 │                                             ▼
                                 │                                   GatedWriter (the gate)
                                 │                                    ON  ─▶ Discord channels
                                 │                                    OFF ─▶ previews table
                                 └─▶ voice-follow, GreetingQueue ─▶ the busiest voice channel
```

## 2. The principles every decision answers to

These are the project's standing rules and anti-goals. When two designs both
worked, the one that served these better won.

1. **Nothing reaches a channel without the owner's approval.** ("Do not send any cards to any
   channels before I approve it.") This outranks everything else.
2. **Low noise.** No historical dump on first start, no double posts after a restart, no new
   message per cycle change, no pings.
3. **Survive quietly.** Restarts, Discord hiccups and source outages must not lose posts,
   duplicate them, or wedge the bot.
4. **No surveillance.** Who was in voice, and when, is never stored or logged.
5. **Official sources first.** Community data only where the official source lacks it.
6. **Real data only in tests.** Fixtures are captured from the real sources. Edge cases are
   derived from those captures. The audio is a real recording, NASA's public-domain Apollo 11 clip.

---

## 3. Decisions and their reasons

### 3.1 One process, one event loop

- **Decision.** A single asyncio process: the Discord client, the pollers, the outbox worker,
  the cycle ticker and the greeting worker all run on one loop.
- **Why.**
  - It's one bot in one small server, so the load is a few HTTP polls every few minutes and a
    handful of voice events.
  - discord.py is asyncio-native, and every other job here is waiting on the network, so one
    loop handles all of it without threads or IPC.
  - One process is one thing to deploy, watch and restart.
- **Considered instead.** Separate worker processes with a message broker; a scheduler such as
  Airflow. Both add moving parts with nothing to gain at this scale.
- **Cost.** A blocking call would stall everything. Mitigations:
  - SQLite runs through `aiosqlite`.
  - FFmpeg runs as a subprocess.
  - HTTP goes through `aiohttp`, with hard deadlines on every request.

### 3.2 A core without Discord, behind small ports

- **Decision.** The rules live in plain Python modules (`news/`, `baro/`, `cycles/`, `voice/`
  classification and queue) that never import discord.py. The rules reach Discord through small
  interfaces, called ports: the channel writer, `VoiceConnector`, `Player` and `KickDetector`.
  Adapters such as `discord_adapter.py` and `discord_sender.py` implement them.
- **Why.**
  - The rules are where the bugs would hurt: a double post, a wrong Baro time, a missed kick. In
    this shape they're tested quickly and directly against real captured data.
  - Discord's quirks stay in one place. Two examples:
    - The DAVE encryption fix of 2026-10-27 changed only the voice adapter and one port
      signature.
    - discord.py's `move_to` undeafens the bot, so Amir never moves himself. That rule is
      enforced in one adapter.
- **Cost.** A layer of indirection. It's kept thin: each port has one or two methods.

### 3.3 Supervised tasks, one per name

- **Decision.** Every background job runs under `TaskSupervisor`. There's at most one task per
  name. A task that crashes restarts with exponential backoff, while one-shot tasks just finish.
- **Why.**
  - A crashing poller must not take the bot down (principle 3).
  - "One per name" makes duplicate work impossible. Two kick checks, or two snapshot refreshes,
    can never run at once.
- **Considered instead.** Bare `asyncio.create_task` calls. An exception in one of those vanishes
  unless someone collects it, and nothing stops a task from being started twice.

### 3.4 The publishing gate is structural, not a flag check

- **Decision.** Every channel write goes through one `GatedWriter`: news, Baro, cycle edits,
  voice-log lines, and `/amir news test` once publishing is on. The only `DiscordSender` is
  created inside the gate, and with publishing off the gate doesn't even hold a reference to it.
- **Why.** Principle 1. An `if publishing:` check scattered through the
  code fails the first time someone forgets one. Here no code path can reach a channel while the
  switch is off, because the object that posts doesn't exist. The deploy script adds a second
  lock: it refuses a config with publishing on unless `--allow-publishing` is given.
- **What happens instead of posting.**
  - The would-be post is stored in the `previews` table. `scripts/render_previews.py --from-db`
    can show exactly what would have gone out.
  - The outbox row is marked `previewed`, a terminal state. So when publishing is turned on,
    **nothing from the off period is replayed**. This is principle 2: turning publishing on
    must not flood the channels.
- **The switch is read once, at startup.** Turning it on is a config change plus a deploy that
  recreates the container. It's never a runtime toggle that a bug or a command could flip.

### 3.5 A durable outbox for news and Baro

- **Decision.** Pollers don't post. They write rows into a SQLite `outbox`, each with a unique
  dedup key: the forum `guid`, the Steam `gid`, a canonical URL, or `baro:<activation>:<milestone>`.
  `OutboxWorker` delivers rows in order and retries with backoff. A row counts as `sent` only
  after Discord confirms the message.
- **Why.**
  - Posting straight from a poller has two crash windows. A crash after posting but before
    recording leads to a **double post** on restart. A crash after recording but before posting
    **loses the post**.
  - The outbox closes both. One transaction records what was seen and what must be posted, so a
    poll is kept entirely or not at all.
  - A crash in the middle of a send leaves the row as `sending`. At the next startup,
    `reconcile_inflight` searches the channel for that card's identity, then marks the row sent
    or queues it again.
- **First-start baseline.** The first poll of any source records everything present as already
  seen and posts nothing. A never-seen item dated more than 7 days back is also recorded without
  being posted, because the forum feed is ordered by activity and old topics can resurface. Both
  rules serve principle 2.
- **Cycle cards don't use the outbox** (§3.7). They are edits to three fixed messages, not a
  stream of posts.

### 3.6 SQLite for state

- **Decision.** One SQLite file (WAL mode) on a named Docker volume. The tables are `outbox`,
  `news_items`, `source_state`, `baro_visits`, `cycle_cards`, `item_names`, `previews` and
  `kv`. Migrations are versioned and every query is parameterised.
- **Why.**
  - The data is tiny: a few hundred rows.
  - There's one writer, Amir.
  - No database server is needed, and a backup is a copy of one file.
  - Amir stays independent of every other project's database, so neither side can break the
    other.
- **Considered instead.** A shared database server. It would couple a Discord bot to a
  production analytics database for no gain.
- **Cost.** Transactional writers share one connection, so `Database.transaction()` serialises
  them behind a lock. The load is small, so this costs nothing in practice.

### 3.7 Three living cycle cards, edited in place

- **Decision.** Exactly three messages in the cycles channel, one each for Plains of Eidolon, Orb Vallis
  and Cambion Drift. Each is posted once, then edited when that world changes state. The
  message IDs are stored, so a restart reuses them. A deleted card is posted again, once.
- **Why.**
  - Each world gets its own card, updated on its own schedule: never one combined card, and
    never all at the same time.
  - A new message per change would mean about 150 posts a day, which breaks principle 2. Orb
    Vallis alone flips about 108 times a day.
  - The countdown uses Discord's relative timestamp (`<t:…:R>`), which every client ticks live.
    No edits are needed between changes.
- **Resilience.**
  - The phase lengths are configured, so the cards roll forward from the last known expiry even
    if WFCD is down.
  - Each world has its own failure backoff, and a failing card warns at most once an hour.
  - A failure streak ends only when a tick actually writes the card, or finds it already
    current. That's the cycle-health fix of 2026-10-27.

### 3.8 Which source for what

| Need | Source | Why this one |
|---|---|---|
| Patch notes and hotfixes | Official forum "PC Update Notes" RSS | The official channel for every update. The RSS isn't behind the forum's Cloudflare challenge, and it has a stable `guid`. |
| Major announcements | Steam Web API, `feeds=steam_community_announcements` | Structured JSON with a stable `gid`, posted by DE staff. Without the `feeds=` filter it also returns third-party press. The Steam store RSS was rejected: it missed an official post. warframe.com has no RSS, and scraping it would duplicate this API. |
| Baro timing and stock | Official world state | Official first. In the 2026-10-26 capture it already listed his next visit about 90 hours ahead, so the 72-hour card can be on time. |
| Item names for Baro's stock | WFCD item database, cached in SQLite and refreshed daily | The official manifest gives only internal paths. After a failed fetch, the cache is retried an hour later. |
| Open-world cycles | WFCD (`api.warframestat.us`) | The official file has no Orb Vallis cycle, and WFCD computes all three. |

- **The announcement filter** is an editable keyword list matched against titles: Baro posts,
  soundtracks, TennoGen, bundles, merch and plush. Every skipped item is logged with the keyword
  that excluded it, so each decision can be explained. Fuzzy matching was rejected for the same
  reason.
- **Being gentle with the sources.**
  - Polling intervals: forum every 5 minutes, Steam every 15, world state every 5, plus a
    refresh 5 seconds after each predicted cycle flip.
  - Conditional requests (ETag and Last-Modified) wherever a source supports them.
  - A descriptive User-Agent, a 5 MB response cap, and hard deadlines on every fetch.

### 3.9 Voice: on demand, with the biggest group

- **Decision.** Amir is in voice only while people are, and he sits in the voice channel with
  the most people. When the first person joins a channel he can sit in, he joins them. When
  nobody is left in any, he waits 60 seconds and then leaves. Other bots never count as people.
- **Why on demand.** An earlier design kept him in a home channel at all times. A bot sitting
  alone in voice is one of the anti-goals.
- **Why the biggest group (2026-10-27).** The group drifts: it starts in Warframe, then most move
  on to other games' channels. Amir stayed behind in an emptying Warframe. A bot account can
  be in only one voice channel per server, so he can't be everywhere: he sits with whichever
  channel has the most users at the moment.
- **No chasing hops.** When another channel gets more people while his own still has people, he
  waits 30 seconds (`follow_settle_seconds`) and moves only if it still has more then. Any voice
  change ends the wait early to decide again; while the same channel stays ahead, only the rest
  of the 30 seconds is left to wait, so a busy server never puts the move off for ever. A tie
  keeps him where he is, so he never bounces back and forth. If his own channel empties, there
  is nobody to leave behind, and he goes at once.
- **Which channels.** Regular voice channels where his role can view, connect and speak. Never
  AFK ("someone parked in AFK isn't really there"), a stage channel (speaking there needs an
  invite), a channel with a user limit (he would take a seat a person needs
  in a three-seat channel), or one in `exclude_channel_ids`. Their people are still logged.
- **Why leave and join, never move.** discord.py 2.7.1's `move_to` sends a voice state without
  the deafen flag, which would undeafen him. So every change of channel is a deliberate leave
  followed by a fresh join, both through the voice session, one at a time. That also tells his
  own moves from a drag, even when Discord's events arrive late: his own move reaches the bot as
  a leave it expects, then a join. A change straight from one channel to another is somebody
  else's drag. If the leave fails, he stays where he is: joining anyway would leave the old
  channel to discord.py, unexpected, and that leave would look like a kick.
- **One decision-maker.** One background task, voice-follow, decides after every voice event, his
  own disconnects, startup, a drag and a release, from a census of every channel. At most one
  runs, so two decisions never race each other to the voice session.
- **Restarts.** If people are already in voice when Amir starts, he joins the biggest group
  quietly: no greetings, no join lines. They didn't just arrive.
- **Self-deafened.** He never receives audio, so there's nothing to record or leak.

### 3.10 Greetings that never get awkward

- **Decision.**
  - One greeting plays at a time, from a queue of at most 5.
  - A greeting older than 30 seconds is dropped rather than played late.
  - One greeting per stay in voice (2026-10-27). A stay runs from joining any voice channel to
    leaving voice altogether, so a hop between channels never earns a second greeting, whoever
    moves. Someone is greeted the first time they share a channel with Amir during their stay:
    when they come into his channel, or when he comes into theirs within 30 seconds of them
    joining voice. People already there longer are not greeted when he moves in.
  - The welcome as he comes into a channel is part of the follow decision that brought him, once
    his connect is done. discord.py reports his own join right after asking for it, before the
    voice handshake is done, while his connection knows no channel yet. A welcome tied to that
    report would race the handshake: whenever the audit log read it starts answered first,
    nobody would be greeted, often the first person of the evening. The report only notes the
    audit log.
  - Someone who rejoins within 2 minutes is not greeted again, though their join is still
    logged.
  - The clip is picked at random, never the same one twice in a row when there are several.
- **Why.** "Awkward audio" is an anti-goal: overlapping greetings, one cutting off another, or a
  "welcome" arriving long after someone joined.
- **Clips.**
  - Every clip is fully decoded by FFmpeg at startup.
  - It counts as valid only if FFmpeg exits cleanly **and** prints no errors. FFmpeg exits 0 on
    a truncated file while still reporting decode errors, so the exit code alone would accept a
    broken clip.
  - The files are never modified.
- **Loudness.**
  - Every clip plays through FFmpeg's `loudnorm`, at **-16 LUFS** with peaks at or below
    -1.5 dBTP.
  - The first target, -20 LUFS, sounded low to the owner. At -16, the owner's clip plays near its
    recorded level as one even gain.
  - This protects ears if a future clip is recorded hot.
- **Waiting for encryption (2026-10-27).**
  - Discord encrypts voice end to end (DAVE) and re-keys the call whenever someone joins. Audio
    sent before the new arrival's client has the key is dropped, and that cut the start of the
    greeting.
  - Amir now waits, checking every 50 ms for at most 5 seconds, until three things are true:
    his session is ready, the arrival is in the encrypted group, and no key change is pending.
    Only then does the clip start.
  - The log records each wait. The first greeting after the fix waited 904 ms. Before the fix,
    the clip started inside that window, so its opening was lost.
  - A test pins the discord.py internals this reads, so a library upgrade that renames them fails
    loudly instead of silently dropping the wait.

### 3.11 Kicks hold, drags pin: telling a kick from an accident

- **The rules (2026-10-27).**
  - Kick: a moderator's kick keeps him out of voice until he is brought back. Any other
    disconnect lets him rejoin.
  - Drag: a drag from one channel to another is deliberate, and means "stay in this channel".
  - His own moves: leaving to follow the busiest channel is his own decision, never a drag or a
    kick.
- **The problem.** Discord doesn't tell a bot who disconnected it. The only witness is the audit
  log, and its `member_disconnect` entries don't name the target. They also aggregate per
  moderator: the count rises while the creation time stays the same.
- **Decision.**
  - **A kick holds.** When Amir joins voice, he takes a snapshot of the latest audit entries.
    After an unexpected disconnect, from any channel, he waits 2 seconds and reads the log again.
    It was a kick if a non-bot entry is new and under 60 seconds old, or if an entry's count went
    up. While he's in voice or connecting to it (every follow move is a connect), the snapshot is
    refreshed 3 seconds after anyone else leaves voice, even from a channel hidden from him, so a
    moderator disconnecting someone else is never mistaken for a kick later. A kick sets a
    **hold**, stored in SQLite with only a time and a reason, no member data. The hold survives
    restarts and blocks every way of joining on his own.
  - **A drag pins.** A change straight from one channel to another that Amir didn't make himself
    is a drag. He stays in that channel, even beside a bigger group, until nobody is left in it,
    then follows the crowd again. Any channel counts except AFK, even one he would never choose,
    such as a capped one; he greets nobody where he can't speak. A drag into an empty channel
    pins nothing. The pin lives in memory, so a restart forgets it. A drag never holds him out.
    This replaces the earlier rule, where a drag held him out like a kick: a hold stored then,
    with the reason `moved`, still holds until it is lifted.
  - **AFK is neither.** Whoever moves him to AFK, a moderator or Discord's idle timer, he leaves
    it and follows the crowd. It sets no hold and pins nothing; to keep him out, kick him.
  - **His own moves are neither.** He leaves and then joins (§3.9), and the session expects that
    leave, so it is never checked for a kick and never taken for a drag.
  - `/amir voice reconnect` lifts a hold and ends a pin. Amir then follows the crowd; the command
    never makes him leave and rejoin on its own.
- **Why it's built so carefully.** Two guarantees shape every path:
  - **Kicked means out.** Greetings and joins wait while a kick check is pending. A snapshot
    taken during a check is thrown away, so a reconnect or a refresh can't hide a kick.
  - **Lifted means back.** A lift clears the hold in memory immediately. Hold saves are
    retried by the companion itself, and stop as soon as the hold is saved or lifted. Hold writes
    take turns, so no retry can write a lifted hold back.
- **Edges accepted and documented.** Amir needs **View Audit Log**: if the log can't be read,
  every disconnect counts as an accident. These rare edges remain:
  - A kick that lands during a gateway-latency window may be missed once. Kicking again works.
    The window now also opens on every follow move: a moderator's kick that lands while Amir is
    leaving a channel on his own is taken for that leave, and he joins the new channel.
  - `/amir voice reconnect` typed within about 2 seconds of the kick is overridden by the kick
    check still in progress. The reply then shows the hold; running it again brings him back.
  - A failed delete during a lift can bring the hold back after a restart. The command reports
    the failure, and running it again fixes it.
  - Discord's own idle move of Amir to AFK ends a moderator's pin, like any move to AFK.
  - Where Amir is comes from his voice connection. While discord.py resumes it or moves it to
    another voice server, a decision made in that moment may misread his channel, and he may drop
    out of voice and rejoin.
  - A greeting to a stage audience may play silently: Discord mutes the audience until it is
    invited to speak.
  - A pinned channel that refuses his rejoin after an accidental drop (full, or a stage channel,
    which he never joins on his own), or a connect that keeps failing for about 105 seconds (the
    full backoff), ends the pin, so he is never kept out of voice by it.

### 3.12 Arrival, departure and move lines: mentions, no pings, no records

- **Decision.** Every voice channel Amir can see is logged, AFK included, in three kinds of line:
  "@Tenno has arrived in #Arcade.", "@Tenno has left #Arcade. Safe travels." and
  "@Tenno has moved from #Warframe to #Arcade." They're built from Discord mentions (`<@id>`,
  `<#id>`) and sent with mentions disabled.
  - A move between two channels is one line, never a departure plus an arrival, so the log shows
    who moved from one game's channel to another.
  - Moves have their own switch, `announce_moves`, next to `announce_joins` and
    `announce_leaves`, all on by default.
  - Discord's own idle move to AFK is logged as an ordinary move. Muting, deafening, streaming
    and camera changes are never lines.
  - Bots are never logged, and Amir never logs himself, his own moves included.
  - A channel hidden from his role can't be seen, so it never appears, though Discord may still
    send its voice updates. The bot treats it as no channel at all: a move into it is a departure
    from the channel he can see, a move out of it an arrival, and anything inside it nothing. The
    census leaves it out too, people and all.
- **Why.**
  - A mention shows each person's current server nickname and stays correct if they rename.
  - Mentions disabled means nobody is ever pinged (principle 2).
  - Amir needs no Members intent to know names. Voice-state events already carry the member and
    both channels.
- **Privacy (principle 4).** The lines are held only in memory until sent. They're never written
  to SQLite and never logged with an ID or a name. While publishing is off they're dropped,
  never stored. If Discord is down they wait, capped at 50, and the oldest are dropped, so an
  outage can't end in a burst of stale lines. Who was in which channel is never recorded: the
  census Amir follows the crowd by, and who has been greeted, live in memory only, and `/amir`
  replies count people per channel without naming them.

### 3.13 Admin commands

- **Decision.** Every command lives under `/amir`. They're registered only for the one server,
  limited to administrators by default, and checked again inside each handler. Every reply is
  ephemeral: only the person who ran the command sees it.
- **Why.** These are operator tools, not features for members. And no command may post to a
  channel while publishing is off. `/amir news test` shows its card only to you until publishing
  is on.

### 3.14 Configuration and secrets

- **Decision.** Everything except the token lives in `config/amir.toml`, validated by Pydantic at
  startup. An unknown key or a bad value stops startup with a clear message that never echoes a
  value. The token comes only from the `DISCORD_TOKEN` environment variable, read from the
  server's `.env`.
- **Why.**
  - A typo should fail loudly before Amir connects, not misbehave quietly.
  - The token must never be in the repo, the image, the config or the logs. The logger also
    redacts anything that looks like it.
  - `--check-config` runs Amir's own loader, so the deploy script checks the exact config the bot
    will read, before copying anything.

### 3.15 Deployment: Docker on the production server, rsync, no git

- **Docker, not systemd.**
  - The image brings its own Python 3.12 and FFmpeg, so nothing depends on the host's packages.
  - The image is built on the server, so it matches the host's architecture.
- **Hardening.**
  - Read-only root filesystem, with `tmpfs` for `/tmp`.
  - A non-root user (UID 10001), `cap_drop: ALL` and `no-new-privileges`.
  - Log rotation: 3 files of 10 MB.
  - A heartbeat health check: healthy means the process is alive and its Discord client is
    ready.
  - `restart: unless-stopped` covers crashes and reboots.
- **rsync, with no git on the server.** This mirrors the owner's other projects.
  - The deploy never copies, overwrites or deletes the server's `.env` or `data/`.
  - It always recreates the container (`--force-recreate`). The config is bind-mounted as a
    single file, and rsync replaces it with a new inode, so a running container would keep
    reading the old one.
- **Rollback.** Each deploy that changes the image keeps the one that was running as
  `amir-beckett:previous`. A rebuild that changes nothing keeps the older rollback intact.
  - The server's containerd image store can't find an image by its ID once a build has moved
    `:latest` off it.
  - So the script tags the outgoing image by name (`:outgoing`) before building. That bug
    surfaced on the second deploy, and the test's fake Docker now behaves like the real one.
- **Deployed on his own.** Amir lives in the fleet's repository but ships in his own container
  with his own deploy script, so a deploy of another bot can never take him down, or the reverse.

### 3.16 Testing: real data only

- **Decision.** Feed, API and world-state tests run against responses captured from the real
  sources on 2026-10-24 (Baro present) and 2026-10-26 (Baro away). Malformed and edge-case inputs
  are derived from those captures at test time. Audio tests use a real recording (NASA's
  public-domain Apollo 11 clip) and copies of it. Discord objects are faked; data never is.
- **Why.** Synthetic data is never used: tests exercise exactly what the real sources send. Real
  captures also caught
  things invented data would have hidden:
  - The official world state is served as `text/html`.
  - The forum feed isn't in date order.
  - FFmpeg exits 0 on a truncated clip.
- **The gate.** At least 96% coverage, plus ruff's lint and format checks, before any commit:
  `scripts/check.sh` at the repository root, the same script CI runs.

---

## 4. Trade-offs accepted

| Trade-off | Why it's acceptable |
|---|---|
| One process means one failure domain. | Supervised tasks restart on their own, Docker restarts the process, and the outbox makes restarts lossless. |
| SQLite allows one writer at a time. | Amir is the only writer, and the load is a few writes a minute. |
| Cycles depend on a community API (WFCD). | The official source lacks Orb Vallis. Cached expiries and configured phase lengths keep the cards right through outages. |
| The DAVE wait reads discord.py internals. | There's no public API for it. A test pins those names against the installed version. |
| The kick check depends on the audit log. | It's the only witness Discord offers. Without the permission, Amir fails safe and rejoins, with one warning per process. |
| Members who leave the server may show as "@unknown-user" in old lines. | Accepted in exchange for live names that update on rename. A frozen text name is a one-line change if preferred. |

## 5. Where the design leaves room to grow

- **A new news source:** one adapter, plus a `[[news.sources]]` entry. Baseline, dedup and the
  outbox come for free.
- **More open worlds** (Earth, Zariman, Duviri are already in the data): configuration plus a
  small mapping.
- **Text-to-speech:** a TTS source would render to a file and go through the same `Player` port
  and greeting queue. See [`tts-evaluation.md`](tts-evaluation.md).
- **Fleet integration** (Atlas health monitoring): `/amir health` and the heartbeat already
  expose the state a monitor would read.
