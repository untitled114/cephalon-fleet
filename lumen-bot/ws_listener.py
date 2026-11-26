"""WebSocket listener: Lumen's real-time nervous system.

Polls /picks/today to discover active games, connects to per-game
WebSocket feeds, and processes both game_update and pick_update messages.
Feeds everything through the Game Context Engine and Intelligence Engine
to generate context-aware alerts.

Also subscribes to /ws/scoreboard for game state transitions (tipoff,
halftime, final) that drive the daily lifecycle, and for Lunara's corrections
of resolved picks.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import websockets

from formatter import PickFormatter
from game_context import AlertType, GameContextEngine, GameState
from game_log import GameLogRecorder
from intelligence import generate_alert_message

log = logging.getLogger("lumen.ws")

# How often to re-poll /picks/today for new games
POLL_INTERVAL = 300  # 5 minutes
EASTERN = ZoneInfo("America/New_York")
# A slate day starts at 6 AM Eastern, when no NBA game is live, so a game that
# ends after midnight still counts toward the night it was played.
SLATE_ROLLOVER_HOUR = 6
# How often to retry a failed WS connection
WS_RETRY_DELAY = 10
# Minimum seconds between DMs to avoid Discord rate limits
DM_COOLDOWN = 3.0
# How often to check for daily recap
RECAP_CHECK_INTERVAL = 300  # 5 minutes
# How often to poll box scores for player minutes/fouls (seconds)
BOXSCORE_POLL_INTERVAL = 45
# How often to poll season stats (only once per player, on first pick load)
SEASON_STATS_FETCHED: set[str] = set()
# The slate whose morning summary went out, kept beside the game logs: every Lunara release
# restarts Lumen, and a slate's summary goes out once.
SUMMARY_SENT_FILE = "logs/summary_sent"


class PickState:
    """Tracks per-pick state to prevent duplicate alerts (legacy path)."""

    __slots__ = (
        "pick_id",
        "last_actual",
        "is_hit",
        "approach_alerted",
        "mid_game_hit_alerted",
    )

    def __init__(self, pick_id: int):
        self.pick_id = pick_id
        self.last_actual: float | None = None
        self.is_hit: bool | None = None
        self.approach_alerted: bool = False
        self.mid_game_hit_alerted: bool = False


def _slate_day(now: datetime | None = None) -> str:
    """The slate day (YYYY-MM-DD) that `now` belongs to, in Eastern time."""
    now = now or datetime.now(EASTERN)
    return (now.astimezone(EASTERN) - timedelta(hours=SLATE_ROLLOVER_HOUR)).date().isoformat()


def _searched_player(matches: list[dict], player_name: str) -> dict | None:
    """The player a /players search found; the search groups its matches by team.

    The pick's own name first, since a search finds every name that contains it; otherwise the
    only match ("Jaren Jackson Jr." for "Jaren Jackson"); with several and none exact, none.
    """
    found = [player for team in matches for player in team.get("players", [])]
    wanted = player_name.strip().lower()
    exact = [p for p in found if p.get("name", "").strip().lower() == wanted]
    if exact:
        return exact[0]
    return found[0] if len(found) == 1 else None


def _summary_slate() -> str:
    """The slate whose summary went out last, or "" when none is on record."""
    try:
        return Path(SUMMARY_SENT_FILE).read_text().strip()
    except OSError:
        return ""


class WSListener:
    def __init__(
        self,
        api_url: str,
        ws_url: str,
        alert_cfg: dict,
        send_dm: Callable,
        formatter: PickFormatter,
    ) -> None:
        self.api_url = api_url.rstrip("/")
        self.ws_url = ws_url.rstrip("/")
        self.alert_cfg = alert_cfg
        self.send_dm = send_dm
        self.formatter = formatter
        self._running = True

        # Legacy pick state for basic alerts
        self._pick_states: dict[int, PickState] = {}
        self._all_picks: list[dict] = []
        # Corrections already sent, as (pick id, actual, is_hit): each comes on the scoreboard
        # and again on the game's socket. Kept across slates, since Lunara re-checks a resolved
        # pick for two days after its game.
        self._corrections_sent: set[tuple] = set()

        # New: Game Context Engine (the brain)
        self.engine = GameContextEngine()
        self.game_log = GameLogRecorder()
        self._logged_state: dict[str, tuple] = {}  # each game's last state written to the log

        # Game WS task management
        self._game_tasks: dict[str, asyncio.Task] = {}
        self._scoreboard_task: asyncio.Task | None = None

        # DM pacing
        self._last_dm_time: float = 0
        self._dm_queue: asyncio.Queue = asyncio.Queue()
        self._dm_worker_task: asyncio.Task | None = None

        # Recap tracking, reset at the start of each slate day
        self._recap_sent: bool = False
        self._summary_sent: bool = False
        self._slate: str = ""

        # Box score poller
        self._boxscore_task: asyncio.Task | None = None

        # Season stats cache (avoid re-fetching)
        self._season_stats_fetched: set[str] = set()

    async def run(self) -> None:
        """Main loop: poll picks, manage WS connections, run intelligence."""
        # Start DM worker (paces outgoing messages)
        self._dm_worker_task = asyncio.create_task(self._dm_worker())

        # Start scoreboard listener
        self._scoreboard_task = asyncio.create_task(self._listen_scoreboard())

        # Start box score poller (for real player minutes + fouls)
        self._boxscore_task = asyncio.create_task(self._poll_box_scores_loop())

        while self._running:
            try:
                await self._poll_and_subscribe()
            except Exception:
                log.exception("Poll cycle error")
            await asyncio.sleep(POLL_INTERVAL)

    async def stop(self) -> None:
        self._running = False
        for task in self._game_tasks.values():
            task.cancel()
        self._game_tasks.clear()
        if self._scoreboard_task:
            self._scoreboard_task.cancel()
        if self._dm_worker_task:
            self._dm_worker_task.cancel()
        if self._boxscore_task:
            self._boxscore_task.cancel()
        self.game_log.close()

    # ── Polling & subscription ──

    async def _poll_and_subscribe(self) -> None:
        """Fetch today's picks, register with engine, subscribe to game WS."""
        self._roll_slate_day()
        picks = await self._fetch_picks()
        if picks is None:
            return

        # Initialize legacy state
        for pick in picks:
            if pick["id"] not in self._pick_states:
                self._pick_states[pick["id"]] = PickState(pick["id"])

        self._all_picks = picks

        # Register picks with the context engine
        self.engine.register_picks(picks)

        # Send morning summary (once per slate day)
        if not self._summary_sent and picks:
            self._summary_sent = True
            self._keep_summary_mark()
            embed = self.formatter.summary_embed(picks)
            await self._queue_dm(embed=embed)
            log.info("Morning summary sent: %d picks", len(picks))

        # Season averages, in the background, for players not looked up yet: every pick after a
        # restart, and picks added after the summary (once per player and game)
        asyncio.create_task(self._fetch_season_stats_for_picks(picks))

        # Subscribe to game WebSockets for games with pending picks
        game_ids = {p["game_id"] for p in picks if p.get("is_hit") is None}
        for game_id in game_ids:
            if game_id not in self._game_tasks or self._game_tasks[game_id].done():
                self._game_tasks[game_id] = asyncio.create_task(self._listen_game(game_id))

        # Clean up finished games
        for gid in list(self._game_tasks):
            if gid not in game_ids:
                self._game_tasks[gid].cancel()
                del self._game_tasks[gid]

        # Check for daily recap
        await self._check_daily_recap()

    async def _fetch_picks(self) -> list[dict] | None:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(f"{self.api_url}/picks/today")
                if resp.status_code != 200:
                    log.warning("Failed to fetch picks: %d", resp.status_code)
                    return None
                data = resp.json()
                return data.get("picks", data) if isinstance(data, dict) else data
        except Exception:
            log.exception("Error fetching picks")
            return None

    # ── WebSocket listeners ──

    async def _listen_game(self, game_id: str) -> None:
        """Connect to WS for a game and process both game_update + pick_update."""
        url = f"{self.ws_url}/{game_id}"
        while self._running:
            try:
                async with websockets.connect(url) as ws:
                    log.info("Connected to WS: %s", game_id)
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                            msg_type = msg.get("type")

                            if msg_type == "pick_update":
                                await self._handle_pick_updates(msg["data"]["picks"])

                            elif msg_type == "game_update":
                                await self._handle_game_update(msg["data"])

                            elif msg_type == "play":
                                await self._handle_play(game_id, msg.get("data", {}))

                            elif msg_type == "history":
                                # Initial play history on connect: bulk process
                                plays = msg.get("data", [])
                                if isinstance(plays, list) and plays:
                                    await self._handle_play_batch(game_id, plays)

                        except (json.JSONDecodeError, KeyError):
                            continue
            except websockets.ConnectionClosed:
                log.info("WS disconnected: %s, reconnecting", game_id)
            except Exception:
                log.exception("WS error for %s", game_id)
            await asyncio.sleep(WS_RETRY_DELAY)

    async def _listen_scoreboard(self) -> None:
        """Connect to scoreboard WS for game lifecycle events."""
        url = f"{self.ws_url}/scoreboard"
        while self._running:
            try:
                async with websockets.connect(url) as ws:
                    log.info("Connected to scoreboard WS")
                    async for raw in ws:
                        try:
                            msg = json.loads(raw)
                            if msg.get("type") == "scoreboard_update":
                                for game_data in msg.get("data", []):
                                    await self._handle_game_update(game_data)
                            elif msg.get("type") == "pick_correction":
                                await self._handle_pick_corrections(msg["data"]["picks"])
                        except (json.JSONDecodeError, KeyError):
                            continue
            except websockets.ConnectionClosed:
                log.info("Scoreboard WS disconnected, reconnecting")
            except Exception:
                log.exception("Scoreboard WS error")
            await asyncio.sleep(WS_RETRY_DELAY)

    # ── Message handlers ──

    async def _handle_game_update(self, data: dict) -> None:
        """Process a game_update through the context engine."""
        alerts = self.engine.update_game(data)

        game_id = data.get("id")
        game = self.engine.games.get(game_id) if game_id else None

        # Log the game's state when it changed, for the games Lumen tracks picks in
        if game and game.picks and game.status in ("live", "halftime", "final"):
            picks = tuple(
                (c.pick_id, c.actual_value, c.is_hit, c.box_score.minutes)
                for c in game.picks.values()
            )
            state = (game.status, game.quarter, game.clock, game.home_score, game.away_score, picks)
            if self._logged_state.get(game.game_id) != state:
                self._logged_state[game.game_id] = state
                self.game_log.record_game_snapshot(game, "game_update")

        # Process intelligent alerts
        for alert_type, game_state, pick_ctx in alerts:
            await self._send_copilot_alert(alert_type, game_state, pick_ctx)

    async def _handle_pick_updates(self, picks: list[dict]) -> None:
        """Process pick updates through both legacy alerts and the context engine."""
        threshold = self.alert_cfg.get("approach_threshold", 2.0)

        # A resolved pick Lunara resolved again is a correction, not a live update or a result.
        await self._handle_pick_corrections([p for p in picks if p.get("correction")])
        picks = [p for p in picks if not p.get("correction")]

        # Feed through context engine for intelligent alerts
        alerts = self.engine.update_picks(picks)
        cleared_early = set()  # crossings the engine just announced: one message per crossing
        for alert_type, game_state, pick_ctx in alerts:
            await self._send_copilot_alert(alert_type, game_state, pick_ctx)
            # Log pick events
            self.game_log.record_pick_update(game_state, pick_ctx, alert_type.name)
            if alert_type == AlertType.LINE_CLEARED_EARLY:
                cleared_early.add(pick_ctx.pick_id)

        # Legacy alert path (approaching, mid-game hit, result)
        for pick in picks:
            pid = pick.get("id")
            if pid is None:
                continue

            state = self._pick_states.get(pid)
            if state is None:
                state = PickState(pid)
                self._pick_states[pid] = state

            actual = pick.get("actual_value")
            is_hit = pick.get("is_hit")
            line = pick.get("line")

            if not line or line <= 0:
                continue

            # Hit/miss (game final)
            if is_hit is not None and state.is_hit is None:
                state.is_hit = is_hit
                if self.alert_cfg.get("hit_miss", True):
                    embed = self.formatter.result_embed(pick)
                    await self._queue_dm(embed=embed)
                    log.info(
                        "Result: %s %s %.1f, %s",
                        pick.get("player_name"),
                        pick.get("market"),
                        actual or 0,
                        "HIT" if is_hit else "MISS",
                    )
                continue

            # Mid-game hit: an OVER clears once it passes the line. An UNDER is decided only at
            # the final (it can still go over), so it never clears mid-game.
            if (
                actual is not None
                and line is not None
                and is_hit is None
                and not state.mid_game_hit_alerted
            ):
                prediction = pick.get("prediction", "").upper()
                cleared = prediction == "OVER" and actual > line
                if cleared:
                    state.mid_game_hit_alerted = True
                    state.approach_alerted = True
                    if pid in cleared_early:
                        continue
                    embed = self.formatter.mid_game_hit_embed(pick)
                    await self._queue_dm(embed=embed)
                    log.info(
                        "Mid-game hit: %s %s %.1f (line: %.1f)",
                        pick.get("player_name"),
                        prediction,
                        actual,
                        line,
                    )
                    state.last_actual = actual
                    continue

            # Approaching line
            if (
                actual is not None
                and line is not None
                and is_hit is None
                and not state.approach_alerted
                and self.alert_cfg.get("approaching_line", True)
            ):
                diff = abs(actual - line)
                if diff <= threshold and actual != state.last_actual:
                    state.approach_alerted = True
                    embed = self.formatter.approaching_embed(pick)
                    await self._queue_dm(embed=embed)
                    log.info(
                        "Approaching: %s at %.1f (line: %.1f)",
                        pick.get("player_name"),
                        actual,
                        line,
                    )

            state.last_actual = actual

    async def _handle_pick_corrections(self, picks: list[dict]) -> None:
        """One DM per corrected result; the engine takes the corrected values for the recap."""
        for pick in picks:
            key = (pick.get("id"), pick.get("actual_value"), pick.get("is_hit"))
            if key in self._corrections_sent:
                continue
            self._corrections_sent.add(key)
            self.engine.update_picks([pick])
            if not self.alert_cfg.get("hit_miss", True):
                continue
            await self._queue_dm(embed=self.formatter.correction_embed(pick))
            log.info(
                "Correction: %s %s %.1f -> %.1f",
                pick.get("player_name"),
                pick.get("market"),
                pick.get("previous_actual") or 0,
                pick.get("actual_value") or 0,
            )

    # ── Play event handlers ──

    async def _handle_play(self, game_id: str, play: dict) -> None:
        """Process a single play event for scoring run / drought detection."""
        alerts = self.engine.update_plays(game_id, [play])
        for alert_type, game_state, pick_ctx in alerts:
            await self._send_copilot_alert(alert_type, game_state, pick_ctx)

    async def _handle_play_batch(self, game_id: str, plays: list[dict]) -> None:
        """Process initial play history: just seed the engine, no alerts."""
        game = self.engine.games.get(game_id)
        if game:
            for play in plays:
                seq = play.get("sequence_number", 0)
                game._play_sequences_seen.add(seq)
            # Keep last 30
            game.recent_plays = plays[-30:] if len(plays) > 30 else plays

    # ── Box score polling ──

    async def _poll_box_scores_loop(self) -> None:
        """Poll box scores for all live games to get real player minutes + fouls."""
        log.info("Box score poller started (interval=%ds)", BOXSCORE_POLL_INTERVAL)
        while self._running:
            try:
                await self._poll_box_scores()
            except Exception:
                log.exception("Box score poll error")
            await asyncio.sleep(BOXSCORE_POLL_INTERVAL)

    async def _poll_box_scores(self) -> None:
        """Fetch box scores for all live games and update pick contexts."""
        for game_id, game in self.engine.games.items():
            if game.status not in ("live", "halftime"):
                continue
            if not game.picks:
                continue

            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    resp = await client.get(f"{self.api_url}/games/{game_id}/boxscore")
                    if resp.status_code != 200:
                        continue
                    data = resp.json()

                # Flatten all players from home + away
                all_players = []
                for side in ("home", "away"):
                    team_data = data.get(side, {})
                    for p in team_data.get("players", []):
                        all_players.append({**p, "team": team_data.get("abbrev")})

                if not all_players:
                    continue

                # Update engine with box score data
                alerts = self.engine.update_box_score(game_id, all_players)
                for alert_type, game_state, pick_ctx in alerts:
                    await self._send_copilot_alert(alert_type, game_state, pick_ctx)

            except Exception:
                log.warning("Box score fetch failed for %s", game_id)

    # ── Season stats fetcher ──

    async def _fetch_season_stats_for_picks(self, picks: list[dict]) -> None:
        """Fetch season averages for players in picks (once per player and game).

        A lookup that fails (an error status or no answer) is tried again on the next poll; one
        that finds no such player is not.
        """
        for pick in picks:
            player_name = pick.get("player_name", "")
            game_id = pick.get("game_id", "")
            if not player_name or not game_id:
                continue

            cache_key = f"{game_id}:{player_name}"
            if cache_key in self._season_stats_fetched:
                continue
            self._season_stats_fetched.add(cache_key)

            retry = True
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    # Search for player to get their ID
                    resp = await client.get(
                        f"{self.api_url}/players",
                        params={"search": player_name},
                    )
                    if resp.status_code != 200:
                        continue

                    player = _searched_player(resp.json(), player_name)
                    if not player:
                        retry = False
                        continue

                    # Fetch stats
                    stats_resp = await client.get(f"{self.api_url}/players/{player['id']}/stats")
                    if stats_resp.status_code != 200:
                        continue

                    stats = stats_resp.json()
                    self.engine.update_season_stats(game_id, player_name, stats)
                    retry = False
                    log.info(
                        "Season stats loaded: %s (%s)",
                        player_name,
                        {k: v for k, v in stats.items() if k in ("ppg", "rpg", "apg")},
                    )

            except Exception:
                log.warning("Season stats fetch failed for %s", player_name)
            finally:
                if retry:
                    self._season_stats_fetched.discard(cache_key)

    # ── Copilot alert dispatch ──

    async def _send_copilot_alert(self, alert_type: AlertType, game: GameState, ctx=None) -> None:
        """Generate and send a copilot-style alert."""
        # Halftime gets a special rich embed
        if alert_type == AlertType.HALFTIME_REPORT:
            embed = self.formatter.halftime_report_embed(game)
            await self._queue_dm(embed=embed)
            log.info(
                "Halftime report: %s @ %s (%d-%d)",
                game.away_team,
                game.home_team,
                game.away_score,
                game.home_score,
            )
            return

        # All other alerts use the intelligence engine text + copilot embed
        message = generate_alert_message(alert_type, game, ctx)
        if message:
            embed = self.formatter.copilot_embed(alert_type, message, game, ctx)
            await self._queue_dm(embed=embed)
            log.info(
                "Copilot alert: %s | %s",
                alert_type.name,
                ctx.player_name if ctx else game.game_id,
            )

    def _roll_slate_day(self) -> None:
        """On a new slate day, re-arm the summary and recap and forget the old slate.

        All of its games go, not just final ones: Lunara's day flips at midnight
        Eastern, so a late game can stop updating while still "live". This slate's
        picks are registered again on the poll that follows.
        """
        today = _slate_day()
        if today == self._slate:
            return
        self._slate = today
        self._summary_sent = _summary_slate() == today
        self._recap_sent = False
        self.engine.start_new_slate()

    def _keep_summary_mark(self) -> None:
        """Note that this slate's summary went out, so a restart doesn't send it again."""
        try:
            path = Path(SUMMARY_SENT_FILE)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(self._slate)
        except OSError:
            log.exception("Summary mark not kept: a restart today would send the summary again")

    # ── Daily recap ──

    async def _check_daily_recap(self) -> None:
        """Send daily recap when all games are final."""
        if self._recap_sent:
            return

        if not self.engine.games:
            return

        # Check if all tracked games are final
        all_final = all(
            g.status == "final"
            for g in self.engine.games.values()
            if g.picks  # only games with picks
        )

        if not all_final:
            return

        resolved = self.engine.get_all_resolved_today()
        if not resolved:
            return

        self._recap_sent = True
        embed = self.formatter.daily_copilot_recap(resolved)
        await self._queue_dm(embed=embed)
        log.info("Daily recap sent: %d resolved picks", len(resolved))

    # ── DM pacing ──

    async def _queue_dm(self, content: str | None = None, embed=None) -> None:
        """Queue a DM to be sent with rate limiting."""
        await self._dm_queue.put((content, embed))

    async def _dm_worker(self) -> None:
        """Process DM queue with cooldown between messages."""
        while self._running:
            try:
                content, embed = await asyncio.wait_for(self._dm_queue.get(), timeout=60)

                # Pace DMs to avoid Discord rate limits
                now = asyncio.get_event_loop().time()
                elapsed = now - self._last_dm_time
                if elapsed < DM_COOLDOWN:
                    await asyncio.sleep(DM_COOLDOWN - elapsed)

                await self.send_dm(content=content, embed=embed)
                self._last_dm_time = asyncio.get_event_loop().time()

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break
            except Exception:
                log.exception("DM worker error")
                await asyncio.sleep(1)
