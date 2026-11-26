# Changelog

All notable changes to Amir Beckett. Versions follow [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Changed

- **Amir follows the crowd.** He used to sit only in Warframe and stayed behind when the group
  moved on to other games' channels.
  - He sits in the voice channel with the most people, joins the first arrival in any channel
    he can join, and leaves 60 s after nobody is left. When another channel gets more people
    while his own still has some, he waits 30 s (`follow_settle_seconds`) and moves only if it
    still has more then, so he never chases a hop or bounces back and forth. A tie keeps him put;
    out of voice, a tie goes to Warframe (`voice_channel_id`).
  - He never chooses the AFK channel, a stage channel, a channel with a user limit,
    or one listed in `exclude_channel_ids` (empty by default). Their people are still logged.
  - He moves by leaving and joining, never with a channel move, so he stays self-deafened. His
    own moves are never logged and never taken for a drag or a kick.
- **The voice log covers every voice channel** Amir can see, AFK included, and a move between two
  channels is one line: "@Tenno has moved from #Warframe to #Arcade." Moves can be switched
  off with `announce_moves` (on by default). Discord's own idle move to AFK is logged as a move.
  A channel hidden from Amir's role is never logged or counted, even though Discord may still
  send its voice updates: a move into it is logged as a departure, a move out of it as an
  arrival.
- **One greeting per stay in voice.** A hop between channels never earns a second greeting,
  whoever moves. Someone is greeted when they come into Amir's channel, or when he comes into
  theirs within 30 s of them joining voice; people already there longer are not greeted. The
  2-minute rejoin window still applies.
- **A drag pins Amir instead of holding him out.** This replaces the drag part of the kick entry
  below. After a drag he stays in that channel, even beside a bigger group, until nobody is left
  in it; a drag into AFK or into an empty channel pins nothing. The pin is kept in memory only.
  A moderator's kick still holds him out, now from any channel, and a hold stored when a drag
  still held him out (reason `moved`) holds until `/amir voice reconnect`.
- **Status and commands name his channel.** `/amir status`, `/amir health` and
  `/amir voice status` say where he is and why: "in #Arcade, with the most people",
  "pinned in #Warframe by a drag since ...", or held out by a kick. `/amir voice status` counts
  people per channel, never naming anyone: "People in voice: #Warframe 3 · #Arcade 1".
  - `/amir voice reconnect` lifts a kick hold and ends a drag's pin, then lets Amir follow the
    crowd, and replies with the channel he is in ("Amir has rejoined voice in #Arcade.").
    It no longer makes him leave and rejoin Warframe on its own, which bounced him through
    Warframe when the group was elsewhere. It waits for his connect, but not for a settle time:
    still with people after a pin ends, he moves to a bigger group 30 s later, and the reply
    names where he is meanwhile.
  - `/amir voice test` plays in whichever channel he is in, and names it. Where his role may not
    speak (pinned there by a drag), it says so instead of queueing a greeting that can't play.
- Patch-notes cards keep the forum post's structure and show more of it, so they are easier to
  read. Headings are bold, list items are bullets with
  nested sub-bullets, paragraphs are their own lines, and images and image descriptions are
  dropped. Whole lines are kept up to 900 characters (was 280 flat), ending with `…` when more
  follows. Cards already posted keep their layout.
- The Baro cards are spaced out, so their text no longer looks clustered. The 72 h
  and 10 h cards have a blank line between their two sentences. The arrival card opens with when
  he leaves, then "His wares, as listed:" with the mods under it, and the "Also aboard" summary
  after a blank line. Cards already posted keep their layout.
- The three cycle cards have a blank line between the state line and the countdown, and every
  state is named with a capital: **Day**, **Night**, **Warm**,
  **Cold**, as **Fass** and **Vome** already were. Each card takes the new layout at its next
  cycle change.
- Greetings play louder: -20 LUFS sounded a little low. The default
  loudness target is now -16 LUFS instead of -20 (`[voice] audio_filter`), about 4 dB louder,
  close to the owner's clip as recorded; peaks stay at or below -1.5 dBTP. An install that sets
  `audio_filter` in its own `config/amir.toml` keeps its value.
- Baro's arrival card lists only his mods by name, one line each with ducat and credit prices,
  in his order. Everything else is summed up in one line of category counts, largest first:
  "Also aboard: Skins and armour 28 · Ship decorations 7 · Other 4 · Glyphs 3 · Weapons 3" for
  the 2026-10-24 visit, which had 56 items. A visit without mods says "No mods this visit." above
  that line; a visit with only mods has no summary line. The late-stock edit uses the same
  layout. The full list was too much to read.
- A moderator's kick keeps Amir out of voice. If a moderator
  disconnects him, or anyone drags him to another channel, he leaves voice and stays out, across
  restarts, until an admin runs `/amir voice reconnect`. Joins and leaves are still posted to the
  voice log meanwhile, but nobody is greeted. Any other disconnect is an accident, and he rejoins
  quietly as before. He no longer moves himself back after a drag, so he is never undeafened by a
  channel move. `/amir status`, `/amir voice status` and `/amir health` show the hold.
  - A kick is recognised from the server's audit log, so Amir now needs **View Audit Log**: the
    invite URL's permission integer is `3230848`. Existing installs must grant it to Amir's role
    by hand; without it every disconnect counts as an accident, with one warning per process.
  - Amir reads the audit log again 3 s after anyone else leaves voice, so a moderator's
    disconnect of someone else is not taken for a kick later. While he checks for a kick,
    greetings and rejoins wait for the answer, so a reconnect can't hide it.
- Clearer wording in Amir's own text. A mod line reads
  "**Primed Fast Hands**: 375 ducats · 120,000 credits", the test card is titled "Test: not a
  real announcement", and the preview page says "Previews only. Nothing has been posted to
  Discord." News titles and excerpts are still quoted as published.

### Fixed

- Fewer failed first attempts when Amir joins voice (seen 2026-10-27: Discord closed 2 of 4 new
  voice sessions with 4006, "session no longer valid", about 60 ms after Amir identified;
  discord.py's retry connected a second later). Amir now pauses 0.5 s before a new voice
  session identifies, so the session can reach Discord's voice server first. A new
  `[logging] voice_debug` setting (off by default) logs discord.py's voice-connection steps,
  with no member data, to diagnose voice connects.
- The start of a greeting is no longer cut off (seen 2026-10-27: only the last part of the clip
  was heard). Discord re-keys a call's end-to-end voice encryption whenever someone
  joins, and Amir started the clip before the arrival could decode it. He now waits until the
  arrival is in the call's encryption and no key change is pending, for at most 5 s, then plays.
  The wait is logged as "greeting waited N ms for the call's encryption".
- `scripts/deploy.sh` no longer stops before restarting Amir when the build changes the image
  (seen on the second deploy, 2026-10-27: "No such image"). The server's containerd image store
  can't find the outgoing image by its id once the build has moved `:latest`, so the script now
  tags it by name (`:outgoing`) before building and turns that into `:previous` afterwards.
- A cycle card whose edits keep failing stays listed as failing in `/amir health` and
  `/amir cycles refresh`, and its failure is a warning at most once an hour. Before, the 30 s
  card check, which had nothing to write, counted as a success: it logged a false "cycle card
  recovered", and the next failure warned again as if new. Now a failure streak ends only when a
  tick writes the card or finds it already up to date, never on that card check alone. The
  retry backoff counts from the end of the failed attempt.

## [0.1.0] - 2026-10-26

First release. **Publishing is off by default.** Nothing reaches a Discord channel until the
owner approves the card previews and turns `publishing.enabled` on.

### Voice companion

- Joins the Warframe voice channel when the first human arrives. Stays while anyone is there,
  and leaves 60 s after the last person goes.
- Greets each arrival with a random pre-recorded clip, never the same one twice in a row.
  - Clips are validated at startup by a full FFmpeg decode.
  - They play through a loudness filter.
  - They go through a bounded serial queue: no overlap, no interruption, and a greeting more
    than 30 s late is dropped.
- A rejoin within 2 minutes is announced but not greeted again.
- After a restart, a disconnect or a drag-out with people present, Amir rejoins quietly, with no
  greeting and no announcement.
- Join and leave lines go to a voice-log channel. They are held in memory only, and their
  mentions never ping.

### Warframe intelligence

- Patch notes and hotfixes from the official forum's PC Update Notes RSS feed.
- Digital Extremes' Steam announcements, with a case-insensitive title exclusion list.
- Silent baseline on each source's first poll. Durable dedup by stable ID or canonical URL, and
  never a historical dump.
- Baro Ki'Teer cards:
  - three per visit: 72 h before arrival, on arrival, and 10 h before departure, with catch-up
    after downtime;
  - the arrival card lists the full stock with names resolved from WFCD's item database, and
    edits that card in place if the stock appears late.
- Three living cycle cards (Plains of Eidolon, Orb Vallis, Cambion Drift), each edited in place,
  with live countdowns.

### Publishing and delivery

- A single `GatedWriter` sits in front of every channel write. While it is off, posts become
  rows in the `previews` table; voice-log lines are dropped.
- A durable SQLite outbox: retries with backoff, and reconciliation after a crash mid-send.
- Sanitising: mentions and markdown neutralised; Unicode preserved. Discord's embed limits are
  enforced. Mentions are always `AllowedMentions.none()`; the optional role ping is off by
  default.

### Administration and operations

- Admin-only `/amir` commands, scoped to the configured guild, with ephemeral replies:
  - `status`, `health`, `version`
  - `voice status|reconnect|test|greetings`
  - `news status|check|sources|test`
  - `config validate`, `cycles refresh`, `baro next`
- Validated TOML config. The token comes only from `DISCORD_TOKEN`. `--check-config` prints a
  redacted report.
- JSON-line logs in America/New_York time, with token redaction and no member identities in
  voice logs.
- Health heartbeat for Docker (`python -m amir.health`).
- `scripts/render_previews.py` renders Discord-style card previews from real captures, or from a
  copy of the server database.
- Docker image: Python 3.12 slim, FFmpeg, non-root UID 10001, read-only root, `/data` volume.
  Plus `compose.yaml`.
- `scripts/deploy.sh`: rsync and a build on the server, recreating the container on every
  deploy so config changes take effect.
  - It never copies `.env*` files (except `.env.example`).
  - It refuses a config with unset Discord IDs.
  - It refuses to deploy with publishing on unless given `--allow-publishing`.
- Documentation: `README.md`, `docs/architecture.md`, and `docs/tts-evaluation.md` (no TTS
  ships).
