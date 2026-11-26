"""Game Context Engine: the brain behind Lumen's game-time intelligence.

Tracks per-game state (score, quarter, clock, pace) and per-player stat
accumulation to generate pace projections, blowout detection, and
intelligent pick commentary.
"""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from dataclasses import dataclass, field
from enum import Enum, auto

log = logging.getLogger("lumen.context")

# NBA constants
REGULATION_MINUTES = 48.0
QUARTER_MINUTES = 12.0
OT_MINUTES = 5.0

# Blowout thresholds
BLOWOUT_DIFF = 20  # Score diff to trigger blowout warning
BLOWOUT_SEVERE = 30  # Score diff for "starters are sitting" alert
GARBAGE_TIME_QUARTER = 4  # Only flag garbage time in Q4+
GARBAGE_TIME_CLOCK = "6:00"  # Under 6 min left in Q4 with big lead

# Pace projection
MIN_MINUTES_FOR_PACE = 6.0  # Don't project pace until 6 game-minutes in
MIN_PLAYER_MINUTES_FOR_PACE = 10.0  # nor from a box score under 10 minutes played


class GamePhase(Enum):
    """Phases of an NBA game for context-aware commentary."""

    PRE_GAME = auto()
    Q1 = auto()
    Q2 = auto()
    HALFTIME = auto()
    Q3 = auto()
    Q4 = auto()
    OT = auto()
    FINAL = auto()


_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
AMBIGUOUS = object()  # more than one player on the list could be the pick's


def name_key(name: str) -> str:
    """A player's name as one key: case, accents, punctuation and a Jr./Sr./II-V suffix don't
    count, so the picks' "Jaime Jaquez" and the league's "Jaime Jaquez Jr." are one player.
    """
    plain = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode()
    joined = re.sub(r"['.\u2019]", "", plain.lower())  # "Kel'el", "P.J." read as one word
    words = re.sub(r"[^a-z0-9]+", " ", joined).split()
    if len(words) > 1 and words[-1] in _SUFFIXES:
        words = words[:-1]
    return " ".join(words)


def match_player(name: str, team: str | None, players: list[dict]) -> dict | object | None:
    """The box-score row of a pick's player, on his team's list when rows say whose list they're on:
    his full name first, then his last name. None if nobody there could be him, AMBIGUOUS if more
    than one could (then neither stats nor a "not playing" call are taken from the box).
    """
    key = name_key(name)
    pool = [p for p in players if not team or not p.get("team") or p.get("team") == team]
    keys = [(p, name_key(p.get("name", ""))) for p in pool]
    exact = [p for p, k in keys if k and k == key]
    if exact:
        return exact[0] if len(exact) == 1 else AMBIGUOUS
    last = key.split()[-1] if key else ""
    same_last = [p for p, k in keys if len(last) > 2 and k.split()[-1:] == [last]]
    if same_last:
        return same_last[0] if len(same_last) == 1 else AMBIGUOUS
    return None


class AlertType(Enum):
    """Types of intelligent alerts Lumen can generate."""

    HALFTIME_REPORT = auto()
    QUARTER_SUMMARY = auto()
    BLOWOUT_WARNING = auto()
    GARBAGE_TIME = auto()
    PACE_COMFORT = auto()  # "on pace for 36, OVER 26.5 looking good"
    PACE_CONCERN = auto()  # "only 4 rebounds at half, needs to pick it up"
    STAT_SURGE = auto()  # player's pace jumped significantly
    LINE_CLEARED_EARLY = auto()  # cleared the line with lots of game left
    FOUL_TROUBLE = auto()  # player in foul trouble, minutes risk
    SCORING_RUN = auto()  # player on a hot streak
    DROUGHT = auto()  # player gone cold
    DAILY_RECAP = auto()
    NOT_PLAYING = auto()  # once per player and game: he is on no list of his team's box


# Foul trouble thresholds
FOUL_TROUBLE_MAP = {
    1: 3,  # 3 fouls in Q1
    2: 4,  # 4 fouls by halftime
    3: 5,  # 5 fouls in Q3
    4: 5,  # 5 fouls in Q4
}

# Minutes-based pace uses 36 min/game average for starters
AVG_STARTER_MINUTES = 36.0


@dataclass
class PlayerSnapshot:
    """Point-in-time snapshot of a player's stats for a pick."""

    timestamp: float
    actual_value: float
    game_minutes_elapsed: float
    quarter: int
    clock: str
    pace_projection: float | None  # projected final value; None until there is one
    score_diff: int  # from the player's team perspective
    player_minutes: float = 0.0  # actual minutes on court (from box score)


@dataclass
class BoxScoreContext:
    """Live box score data for a tracked player."""

    minutes: float = 0.0  # actual minutes played (parsed from "35:42")
    fouls: int = 0  # personal fouls (0-6)
    fg_made: int = 0
    fg_attempted: int = 0
    three_made: int = 0
    three_attempted: int = 0
    ft_made: int = 0
    ft_attempted: int = 0
    plus_minus: int = 0
    starter: bool = False
    last_updated: float = 0.0  # timestamp of last box score poll


@dataclass
class ScoringRun:
    """Tracks a scoring run/drought for a player."""

    player_name: str
    team: str
    stat_type: str  # "scoring_run" or "drought"
    start_value: float  # stat value when run/drought started
    start_clock: str
    start_quarter: int
    duration_plays: int = 0
    duration_seconds: float = 0.0


@dataclass
class PickContext:
    """Rich context for a single pick, tracked over the game."""

    pick_id: int
    player_name: str
    team: str
    opponent_team: str
    market: str
    line: float
    prediction: str  # OVER or UNDER
    tier: str
    model_version: str
    is_home: bool

    # Live tracking
    actual_value: float = 0.0
    valued: bool = False  # the feed has sent the pick's value at least once
    is_hit: bool | None = None
    snapshots: list[PlayerSnapshot] = field(default_factory=list)

    # Box score context (real player minutes, fouls, shooting)
    box_score: BoxScoreContext = field(default_factory=BoxScoreContext)

    # Season averages (fetched once from /players/{id}/stats)
    season_avg: float | None = None  # ppg/rpg/apg matching the market
    player_api_id: str | None = None  # ESPN player ID for stats lookup
    not_playing: bool = False  # his team's box score lists him nowhere: out tonight

    # Alert dedup
    halftime_reported: bool = False
    blowout_alerted: bool = False
    garbage_time_alerted: bool = False
    pace_concern_alerted: bool = False
    pace_comfort_alerted: bool = False
    line_cleared_alerted: bool = False
    foul_trouble_alerted: bool = False
    scoring_run_alerted: bool = False
    drought_alerted: bool = False
    quarter_summaries_sent: set[int] = field(default_factory=set)

    @property
    def latest_pace(self) -> float | None:
        if self.snapshots:
            return self.snapshots[-1].pace_projection
        return None

    @property
    def comfort_level(self) -> str:
        """How comfortable the pick is looking: crushing, comfortable, on_track, tight, behind, buried."""
        if self.is_hit is not None:
            return "hit" if self.is_hit else "miss"

        pace = self.latest_pace
        if pace is None:
            return "unknown"

        if self.prediction == "OVER":
            diff = pace - self.line
            if diff > self.line * 0.3:
                return "crushing"
            if diff > self.line * 0.15:
                return "comfortable"
            if diff > 0:
                return "on_track"
            if diff > -self.line * 0.1:
                return "tight"
            if diff > -self.line * 0.25:
                return "behind"
            return "buried"
        else:  # UNDER
            diff = self.line - pace
            if diff > self.line * 0.3:
                return "crushing"
            if diff > self.line * 0.15:
                return "comfortable"
            if diff > 0:
                return "on_track"
            if diff > -self.line * 0.1:
                return "tight"
            if diff > -self.line * 0.25:
                return "behind"
            return "buried"


@dataclass
class GameState:
    """Full state of a single game, updated in real time."""

    game_id: str
    home_team: str
    away_team: str
    home_score: int = 0
    away_score: int = 0
    status: str = "scheduled"
    quarter: int = 0
    clock: str = "12:00"
    venue: str = ""

    # Phase tracking
    prev_quarter: int = 0
    prev_status: str = "scheduled"
    halftime_processed: bool = False

    # Timestamps
    first_update_ts: float = 0.0
    last_update_ts: float = 0.0

    # Picks for this game
    picks: dict[int, PickContext] = field(default_factory=dict)
    told_not_playing: set[str] = field(default_factory=set)  # players already DM'd

    @property
    def score_diff(self) -> int:
        """Home score - away score."""
        return self.home_score - self.away_score

    @property
    def abs_diff(self) -> int:
        return abs(self.score_diff)

    @property
    def leading_team(self) -> str:
        if self.home_score > self.away_score:
            return self.home_team
        elif self.away_score > self.home_score:
            return self.away_team
        return "TIE"

    @property
    def phase(self) -> GamePhase:
        if self.status == "final":
            return GamePhase.FINAL
        if self.status == "halftime":
            return GamePhase.HALFTIME
        if self.status == "scheduled":
            return GamePhase.PRE_GAME
        if self.quarter == 1:
            return GamePhase.Q1
        if self.quarter == 2:
            return GamePhase.Q2
        if self.quarter == 3:
            return GamePhase.Q3
        if self.quarter == 4:
            return GamePhase.Q4
        return GamePhase.OT

    @property
    def is_blowout(self) -> bool:
        return self.abs_diff >= BLOWOUT_DIFF and self.quarter >= 3

    @property
    def is_garbage_time(self) -> bool:
        if self.abs_diff < BLOWOUT_SEVERE:
            return False
        if self.quarter < GARBAGE_TIME_QUARTER:
            return False
        return _clock_to_seconds(self.clock) < _clock_to_seconds(GARBAGE_TIME_CLOCK)

    @property
    def game_minutes_elapsed(self) -> float:
        """Estimate total game minutes elapsed from quarter + clock."""
        if self.status == "halftime":
            return 24.0
        if self.status == "final":
            return REGULATION_MINUTES
        if self.status == "scheduled":
            return 0.0

        q = max(1, self.quarter)
        clock_secs = _clock_to_seconds(self.clock)
        quarter_secs = QUARTER_MINUTES * 60

        # Minutes completed in previous quarters
        completed = (q - 1) * QUARTER_MINUTES
        # Minutes elapsed in current quarter
        current = (quarter_secs - clock_secs) / 60.0

        return completed + current

    def score_diff_for_team(self, team: str) -> int:
        """Get score differential from a specific team's perspective."""
        if team == self.home_team:
            return self.home_score - self.away_score
        return self.away_score - self.home_score

    # Play-by-play tracking (for scoring runs / droughts)
    recent_plays: list[dict] = field(default_factory=list)  # last ~20 plays
    _play_sequences_seen: set[int] = field(default_factory=set)

    def team_score(self, team: str) -> int:
        if team == self.home_team:
            return self.home_score
        return self.away_score

    def opponent_score(self, team: str) -> int:
        if team == self.home_team:
            return self.away_score
        return self.home_score


def _clock_to_seconds(clock: str) -> float:
    """Convert '4:30' to 270.0 seconds."""
    if not clock:
        return 0.0
    try:
        parts = clock.split(":")
        if len(parts) == 2:
            return float(parts[0]) * 60 + float(parts[1])
        return float(parts[0])
    except (ValueError, IndexError):
        return 0.0


def calculate_pace_projection(
    actual_value: float,
    game_minutes_elapsed: float,
    total_minutes: float = REGULATION_MINUTES,
    player_minutes: float = 0.0,
) -> float | None:
    """Project a player's final stat value based on current pace.

    Two modes:
    1. If player_minutes > 0 (from box score): use actual on-court minutes
       with AVG_STARTER_MINUTES as total. More accurate: accounts for bench time.
       Under MIN_PLAYER_MINUTES_FOR_PACE played there is no projection: 2 rebounds in
       5 minutes read as "on pace for 14".
    2. Fallback, with no box score yet: use game clock minutes with REGULATION_MINUTES as total.

    Returns None if not enough time has elapsed for a meaningful projection.
    """
    if player_minutes >= MIN_PLAYER_MINUTES_FOR_PACE:
        # Use real minutes: project based on expected total minutes
        rate = actual_value / player_minutes
        return round(rate * AVG_STARTER_MINUTES, 1)
    if player_minutes > 0:
        return None

    if game_minutes_elapsed < MIN_MINUTES_FOR_PACE:
        return None

    rate = actual_value / game_minutes_elapsed
    return round(rate * total_minutes, 1)


def parse_minutes_str(minutes_str: str) -> float:
    """Parse ESPN minutes string like '35:42' or '12' to float minutes."""
    if not minutes_str:
        return 0.0
    try:
        if ":" in minutes_str:
            parts = minutes_str.split(":")
            return float(parts[0]) + float(parts[1]) / 60.0
        return float(minutes_str)
    except (ValueError, IndexError):
        return 0.0


def parse_shot_str(shot_str: str) -> tuple[int, int]:
    """Parse '12-24' to (made, attempted)."""
    if not shot_str:
        return (0, 0)
    try:
        parts = shot_str.split("-")
        return (int(parts[0]), int(parts[1])) if len(parts) == 2 else (0, 0)
    except (ValueError, IndexError):
        return (0, 0)


class GameContextEngine:
    """Manages game state and pick context across all active games.

    This is the central intelligence hub. It processes raw game updates
    and pick updates, maintaining rich context that the intelligence
    engine uses to generate commentary.
    """

    def __init__(self) -> None:
        self.games: dict[str, GameState] = {}
        self._resolved_pick_ids: set[int] = set()

    def start_new_slate(self) -> None:
        """Forget the previous slate: every game and resolved pick, finished or not."""
        self.games.clear()
        self._resolved_pick_ids.clear()

    def register_picks(self, picks: list[dict]) -> None:
        """Register picks from the /picks/today API response."""
        for pick in picks:
            game_id = pick.get("game_id")
            pick_id = pick.get("id")
            if not game_id or not pick_id:
                continue
            if pick_id in self._resolved_pick_ids:
                continue

            # Ensure game state exists
            if game_id not in self.games:
                self.games[game_id] = GameState(
                    game_id=game_id,
                    home_team=pick.get("home_team", ""),
                    away_team=pick.get("away_team", ""),
                )

            game = self.games[game_id]

            # Register pick context if not already tracked
            if pick_id not in game.picks:
                game.picks[pick_id] = PickContext(
                    pick_id=pick_id,
                    player_name=pick.get("player_name", ""),
                    team=pick.get("team", ""),
                    opponent_team=pick.get("opponent_team", ""),
                    market=pick.get("market", ""),
                    line=float(pick.get("line") or 0),
                    prediction=pick.get("prediction", "OVER"),
                    tier=pick.get("tier", ""),
                    model_version=pick.get("model_version", ""),
                    is_home=bool(pick.get("is_home")),
                )

    def update_game(self, data: dict) -> list[tuple[AlertType, GameState, PickContext | None]]:
        """Process a game_update message. Returns triggered alerts."""
        game_id = data.get("id")
        if not game_id:
            return []

        game = self.games.get(game_id)
        if not game:
            game = GameState(
                game_id=game_id,
                home_team=data.get("home_team", ""),
                away_team=data.get("away_team", ""),
            )
            self.games[game_id] = game

        # Save previous state for transition detection
        game.prev_quarter = game.quarter
        game.prev_status = game.status

        # Update game state
        game.home_team = data.get("home_team", game.home_team)
        game.away_team = data.get("away_team", game.away_team)
        game.home_score = int(data.get("home_score", game.home_score) or 0)
        game.away_score = int(data.get("away_score", game.away_score) or 0)
        game.status = data.get("status", game.status)
        game.quarter = int(data.get("quarter", game.quarter) or 0)
        game.clock = data.get("clock", game.clock) or "0:00"
        game.venue = data.get("venue", game.venue)

        now = time.time()
        if game.first_update_ts == 0:
            game.first_update_ts = now
        game.last_update_ts = now

        alerts: list[tuple[AlertType, GameState, PickContext | None]] = []
        alerts.extend(self._check_game_alerts(game))
        for ctx in game.picks.values():
            alerts.extend(self._refresh_pace(game, ctx))
        return alerts

    def update_picks(self, picks: list[dict]) -> list[tuple[AlertType, GameState, PickContext]]:
        """Process a pick_update message. Returns triggered alerts."""
        alerts = []

        for pick_data in picks:
            pick_id = pick_data.get("id")
            game_id = pick_data.get("game_id")
            if not pick_id or not game_id:
                continue

            game = self.games.get(game_id)
            if not game:
                continue

            ctx = game.picks.get(pick_id)
            if not ctx:
                continue

            actual = pick_data.get("actual_value")
            is_hit = pick_data.get("is_hit")

            if actual is not None:
                ctx.actual_value = float(actual)
                ctx.valued = True

            if is_hit is not None:
                ctx.is_hit = is_hit
                self._resolved_pick_ids.add(pick_id)

            if actual is not None:
                alerts.extend(self._refresh_pace(game, ctx))

        return alerts

    def _refresh_pace(
        self, game: GameState, ctx: PickContext
    ) -> list[tuple[AlertType, GameState, PickContext]]:
        """Project the pick from where it stands now, then check its pace alerts.

        Runs when the pick's value, the player's minutes or the clock moves: a stat that stands
        still while the minutes run slows the pace. A pick the feed hasn't valued yet has no pace.
        """
        if ctx.is_hit is not None or ctx.not_playing or game.game_minutes_elapsed <= 0:
            return []
        if not ctx.snapshots and not ctx.valued:
            return []
        last = ctx.snapshots[-1] if ctx.snapshots else None
        now = (ctx.actual_value, game.game_minutes_elapsed, ctx.box_score.minutes)
        if last and (last.actual_value, last.game_minutes_elapsed, last.player_minutes) == now:
            return []
        pace = calculate_pace_projection(
            ctx.actual_value, game.game_minutes_elapsed, player_minutes=ctx.box_score.minutes
        )
        ctx.snapshots.append(
            PlayerSnapshot(
                timestamp=time.time(),
                actual_value=ctx.actual_value,
                game_minutes_elapsed=game.game_minutes_elapsed,
                quarter=game.quarter,
                clock=game.clock,
                pace_projection=pace,
                score_diff=game.score_diff_for_team(ctx.team),
                player_minutes=ctx.box_score.minutes,
            )
        )
        return self._check_pace_alerts(game, ctx)

    def get_halftime_picks(self, game_id: str) -> list[PickContext]:
        """Get all unreported picks for a game at halftime."""
        game = self.games.get(game_id)
        if not game:
            return []
        return [
            ctx for ctx in game.picks.values() if not ctx.halftime_reported and ctx.is_hit is None
        ]

    def get_quarter_picks(self, game_id: str, quarter: int) -> list[PickContext]:
        """Get picks that haven't had a summary for this quarter."""
        game = self.games.get(game_id)
        if not game:
            return []
        return [
            ctx
            for ctx in game.picks.values()
            if quarter not in ctx.quarter_summaries_sent and ctx.is_hit is None
        ]

    def get_all_resolved_today(self) -> list[PickContext]:
        """Get all resolved picks across all games."""
        resolved = []
        for game in self.games.values():
            for ctx in game.picks.values():
                if ctx.is_hit is not None:
                    resolved.append(ctx)
        return resolved

    def _check_game_alerts(
        self, game: GameState
    ) -> list[tuple[AlertType, GameState, PickContext | None]]:
        """Check for game-level alerts (halftime, quarter change, blowout)."""
        alerts: list[tuple[AlertType, GameState, PickContext | None]] = []

        # Halftime transition. The report lists the picks still open, so a game
        # without any sends none.
        if (
            game.status == "halftime"
            and game.prev_status != "halftime"
            and not game.halftime_processed
        ):
            game.halftime_processed = True
            open_picks = [ctx for ctx in game.picks.values() if ctx.is_hit is None]
            for ctx in open_picks:
                ctx.halftime_reported = True
            if open_picks:
                alerts.append((AlertType.HALFTIME_REPORT, game, None))

        # Quarter transition. The halftime report already covered the end of the 2nd quarter.
        after_halftime = game.prev_quarter == 2 and game.halftime_processed
        if (
            game.quarter > game.prev_quarter
            and game.prev_quarter > 0
            and game.status == "live"
            and not after_halftime
        ):
            alerts.append((AlertType.QUARTER_SUMMARY, game, None))

        # Blowout detection
        if game.is_blowout:
            for ctx in game.picks.values():
                # Only alert for picks on the LEADING team (star might sit)
                if (
                    ctx.is_hit is None
                    and not ctx.not_playing
                    and not ctx.blowout_alerted
                    and ctx.team == game.leading_team
                ):
                    ctx.blowout_alerted = True
                    alerts.append((AlertType.BLOWOUT_WARNING, game, ctx))

        # Garbage time
        if game.is_garbage_time:
            for ctx in game.picks.values():
                if (
                    ctx.is_hit is None
                    and not ctx.not_playing
                    and not ctx.garbage_time_alerted
                    and ctx.team == game.leading_team
                ):
                    ctx.garbage_time_alerted = True
                    alerts.append((AlertType.GARBAGE_TIME, game, ctx))

        return alerts

    def update_box_score(
        self, game_id: str, players: list[dict]
    ) -> list[tuple[AlertType, GameState, PickContext]]:
        """Update pick contexts with real box score data (minutes, fouls, shooting).

        `players` is a list of dicts with: name, minutes, fouls, fg, three_pt, ft,
        plus_minus, starter (matching the BoxScorePlayer schema) and team, the abbreviation
        of the side that lists him. A pick whose team is listed without him is not playing.
        """
        game = self.games.get(game_id)
        if not game:
            return []

        alerts: list[tuple[AlertType, GameState, PickContext]] = []
        now = time.time()

        for ctx in game.picks.values():
            if ctx.is_hit is not None:
                continue

            data = match_player(ctx.player_name, ctx.team, players)
            if data is AMBIGUOUS:
                continue  # two could be him: take nothing from the box for this pick
            if data is None:
                ctx.not_playing = any(p.get("team") == ctx.team for p in players)
                if ctx.not_playing and ctx.player_name not in game.told_not_playing:
                    game.told_not_playing.add(ctx.player_name)
                    alerts.append((AlertType.NOT_PLAYING, game, ctx))
                continue
            ctx.not_playing = False

            # Update box score context
            bs = ctx.box_score
            bs.minutes = parse_minutes_str(data.get("minutes", ""))
            bs.fouls = int(data.get("fouls", 0) or 0)
            bs.fg_made, bs.fg_attempted = parse_shot_str(data.get("fg", ""))
            bs.three_made, bs.three_attempted = parse_shot_str(data.get("three_pt", ""))
            bs.ft_made, bs.ft_attempted = parse_shot_str(data.get("ft", ""))
            bs.starter = bool(data.get("starter", False))
            bs.last_updated = now

            try:
                pm = data.get("plus_minus", "0")
                bs.plus_minus = int(pm) if pm else 0
            except (ValueError, TypeError):
                bs.plus_minus = 0

            # Store player API ID if available
            if data.get("player_id"):
                ctx.player_api_id = str(data["player_id"])

            # Check foul trouble, then the pace at the new minutes
            alerts.extend(self._check_foul_trouble(game, ctx))
            alerts.extend(self._refresh_pace(game, ctx))

        return alerts

    def update_plays(
        self, game_id: str, plays: list[dict]
    ) -> list[tuple[AlertType, GameState, PickContext]]:
        """Process play-by-play events for scoring runs and droughts."""
        game = self.games.get(game_id)
        if not game:
            return []

        alerts: list[tuple[AlertType, GameState, PickContext]] = []

        for play in plays:
            seq = play.get("sequence_number", 0)
            if seq in game._play_sequences_seen:
                continue
            game._play_sequences_seen.add(seq)

            game.recent_plays.append(play)
            # Keep only last 30 plays
            if len(game.recent_plays) > 30:
                game.recent_plays = game.recent_plays[-30:]

        # Check for scoring runs / droughts
        alerts.extend(self._check_momentum(game))

        return alerts

    def update_season_stats(self, game_id: str, player_name: str, stats: dict) -> None:
        """Store season averages for a player's picks."""
        game = self.games.get(game_id)
        if not game:
            return

        pname = player_name.strip().lower()
        for ctx in game.picks.values():
            if ctx.player_name.strip().lower() != pname:
                continue
            # Map market to stat key
            stat_key = {
                "POINTS": "ppg",
                "REBOUNDS": "rpg",
                "ASSISTS": "apg",
                "STEALS": "spg",
                "BLOCKS": "bpg",
            }.get(ctx.market)
            if stat_key and stat_key in stats:
                try:
                    ctx.season_avg = float(stats[stat_key])
                except (ValueError, TypeError):
                    pass

    def _check_foul_trouble(
        self, game: GameState, ctx: PickContext
    ) -> list[tuple[AlertType, GameState, PickContext]]:
        """Check if a player is in foul trouble."""
        alerts: list[tuple[AlertType, GameState, PickContext]] = []

        if ctx.foul_trouble_alerted or ctx.is_hit is not None:
            return alerts

        fouls = ctx.box_score.fouls
        quarter = game.quarter
        threshold = FOUL_TROUBLE_MAP.get(quarter, 5)

        if fouls >= threshold:
            ctx.foul_trouble_alerted = True
            alerts.append((AlertType.FOUL_TROUBLE, game, ctx))

        return alerts

    def _check_momentum(self, game: GameState) -> list[tuple[AlertType, GameState, PickContext]]:
        """Detect scoring runs and droughts from play-by-play data."""
        alerts: list[tuple[AlertType, GameState, PickContext]] = []

        if len(game.recent_plays) < 5:
            return alerts

        # For each tracked player, count their scoring plays in the last 10 plays
        last_n = game.recent_plays[-10:]

        for ctx in game.picks.values():
            if ctx.is_hit is not None or ctx.market != "POINTS":
                continue

            pname = ctx.player_name.strip().lower()
            scoring_plays = 0
            for play in last_n:
                play_player = (play.get("player_name") or "").strip().lower()
                event = (play.get("event_type") or "").lower()
                desc = (play.get("description") or "").lower()

                # Match player name (exact or last name) and a scoring event
                is_player = play_player == pname or (
                    pname.split()[-1] in play_player and len(pname.split()[-1]) > 2
                )
                is_score = (
                    any(kw in event for kw in ("shot", "dunk", "layup", "3pt", "free throw"))
                    or "makes" in desc
                    or "made" in desc
                )
                if is_player and is_score:
                    scoring_plays += 1

            # Scoring run: 4+ scoring plays in last 10
            if scoring_plays >= 4 and not ctx.scoring_run_alerted:
                ctx.scoring_run_alerted = True
                alerts.append((AlertType.SCORING_RUN, game, ctx))

            # Drought: check if player has 0 scoring plays in last 10
            # and we're past Q1 and their stat hasn't moved
            if (
                scoring_plays == 0
                and len(ctx.snapshots) >= 2
                and not ctx.drought_alerted
                and game.game_minutes_elapsed >= 18
            ):
                # Check if actual_value hasn't changed in recent snapshots
                recent = ctx.snapshots[-3:]
                if len(recent) >= 2 and all(
                    s.actual_value == recent[0].actual_value for s in recent
                ):
                    ctx.drought_alerted = True
                    alerts.append((AlertType.DROUGHT, game, ctx))

        return alerts

    def _check_pace_alerts(
        self, game: GameState, ctx: PickContext
    ) -> list[tuple[AlertType, GameState, PickContext]]:
        """Check pace-based alerts for a specific pick."""
        alerts: list[tuple[AlertType, GameState, PickContext]] = []

        if ctx.is_hit is not None:
            return alerts

        pace = ctx.latest_pace
        if pace is None:
            return alerts

        elapsed = game.game_minutes_elapsed
        comfort = ctx.comfort_level

        # Pace comfort: only alert once, and only past Q1
        if (
            comfort in ("crushing", "comfortable")
            and not ctx.pace_comfort_alerted
            and elapsed >= 14
        ):
            ctx.pace_comfort_alerted = True
            alerts.append((AlertType.PACE_COMFORT, game, ctx))

        # Pace concern: alert once, past halftime
        if comfort in ("behind", "buried") and not ctx.pace_concern_alerted and elapsed >= 24:
            ctx.pace_concern_alerted = True
            alerts.append((AlertType.PACE_CONCERN, game, ctx))

        # Line cleared early (still lots of game left)
        if not ctx.line_cleared_alerted and elapsed < 40:
            if ctx.prediction == "OVER" and ctx.actual_value > ctx.line:
                ctx.line_cleared_alerted = True
                alerts.append((AlertType.LINE_CLEARED_EARLY, game, ctx))
            elif ctx.prediction == "UNDER" and ctx.actual_value < ctx.line and elapsed >= 36:
                # UNDER cleared = game almost over and still under
                ctx.line_cleared_alerted = True
                alerts.append((AlertType.LINE_CLEARED_EARLY, game, ctx))

        return alerts
