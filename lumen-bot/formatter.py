"""Discord embed formatters for Cephalon Lumen: Game-Time Copilot.

Builds rich Discord embeds for every alert type: halftime reports,
blowout warnings, pace projections, quarter summaries, and results.
"""

from __future__ import annotations

import discord

from game_context import AlertType, GameState, PickContext

# Tier colors
TIER_COLORS = {
    "X": 0xFFD700,  # Gold
    "Z": 0x7C3AED,  # Purple
    "META": 0x22C55E,  # Green
    "star_tier": 0xF59E0B,  # Amber
    "A": 0x6B7280,  # Gray
}

# Alert type colors
ALERT_COLORS = {
    AlertType.HALFTIME_REPORT: 0x3B82F6,  # Blue
    AlertType.QUARTER_SUMMARY: 0x6366F1,  # Indigo
    AlertType.BLOWOUT_WARNING: 0xF97316,  # Orange
    AlertType.GARBAGE_TIME: 0xEF4444,  # Red
    AlertType.PACE_COMFORT: 0x22C55E,  # Green
    AlertType.PACE_CONCERN: 0xEAB308,  # Yellow
    AlertType.LINE_CLEARED_EARLY: 0x10B981,  # Emerald
    AlertType.FOUL_TROUBLE: 0xF97316,  # Orange
    AlertType.SCORING_RUN: 0xF59E0B,  # Amber
    AlertType.DROUGHT: 0x6366F1,  # Indigo
    AlertType.DAILY_RECAP: 0x8B5CF6,  # Violet
    AlertType.NOT_PLAYING: 0x6B7280,  # Gray
}

DEFAULT_COLOR = 0x3B82F6  # Blue


def _v(pick: dict, key: str, default: str = "?") -> str:
    """Get a pick value, treating None the same as missing."""
    val = pick.get(key)
    return val if val is not None else default


RECAP_COLOR = 0x3B82F6  # one colour for every recap: the numbers speak for themselves


def _comfort_bar(comfort: str) -> str:
    """Visual comfort indicator."""
    bars = {
        "crushing": "\u2588\u2588\u2588\u2588\u2588",
        "comfortable": "\u2588\u2588\u2588\u2588\u2591",
        "on_track": "\u2588\u2588\u2588\u2591\u2591",
        "tight": "\u2588\u2588\u2591\u2591\u2591",
        "behind": "\u2588\u2591\u2591\u2591\u2591",
        "buried": "\u2591\u2591\u2591\u2591\u2591",
    }
    return bars.get(comfort, "\u2591\u2591\u2591\u2591\u2591")


def _quarter_label(q: int) -> str:
    if q <= 4:
        return {1: "Q1", 2: "Q2", 3: "Q3", 4: "Q4"}.get(q, f"Q{q}")
    return f"OT{q - 4}" if q > 5 else "OT"


class PickFormatter:
    """Builds Discord embeds for all Lumen alert types."""

    # ── Legacy dict-based embeds (still used for basic pick_update alerts) ──

    def summary_embed(self, picks: list[dict]) -> discord.Embed:
        """Morning summary embed for new picks."""
        embed = discord.Embed(
            title="Lumen: Today's Picks",
            color=0x22C55E,
            description=f"**{len(picks)}** picks loaded | Game-Time Copilot active",
        )

        by_tier: dict[str, list[dict]] = {}
        for pick in picks:
            tier = pick.get("tier", "?")
            by_tier.setdefault(tier, []).append(pick)

        for tier in ["X", "Z", "META", "star_tier", "A"]:
            tier_picks = by_tier.get(tier, [])
            if not tier_picks:
                continue

            lines = []
            for p in tier_picks:
                model = p.get("model_version", "?").upper()
                injury = ""
                if p.get("injury_status"):
                    injury = f" | injury: {p['injury_status']}"
                lines.append(
                    f"**{p.get('player_name', '?')}** "
                    f"{p.get('prediction', '?')} {p.get('line', '?')} {p.get('market', '?')}"
                    f" [{model}]{injury}"
                )

            embed.add_field(
                name=f"{tier} Tier ({len(tier_picks)})",
                value="\n".join(lines[:10]),
                inline=False,
            )

        embed.set_footer(text="Cephalon Lumen | Game-Time Copilot")
        return embed

    def approaching_embed(self, pick: dict) -> discord.Embed:
        """Alert when a player's stat is approaching their line."""
        actual = pick.get("actual_value") or 0
        line = pick.get("line") or 0
        prediction = _v(pick, "prediction", "OVER")
        market = _v(pick, "market")
        tier = _v(pick, "tier")
        model = _v(pick, "model_version").upper()

        if prediction == "OVER":
            remaining = line - actual
            status = f"needs **{remaining:.1f}** more" if remaining > 0 else "**over the line**"
        else:
            remaining = actual - line
            status = f"**{remaining:.1f}** over the line" if remaining > 0 else "**under the line**"

        color = TIER_COLORS.get(tier, DEFAULT_COLOR)
        ctx_parts = [p for p in [tier, model] if p and p != "?"]
        ctx_line = " | ".join(ctx_parts)

        embed = discord.Embed(
            title=f"{_v(pick, 'player_name')}: Approaching",
            description=(
                f"**{prediction} {line} {market}**\n"
                f"Currently at **{actual:.1f}**, {status}\n"
                f"{ctx_line}"
            ),
            color=color,
        )
        embed.set_footer(text="Cephalon Lumen | Game-Time Copilot")
        return embed

    def mid_game_hit_embed(self, pick: dict) -> discord.Embed:
        """Alert when a pick clears its line during a live game."""
        actual = pick.get("actual_value") or 0
        line = pick.get("line") or 0
        market = _v(pick, "market")
        prediction = _v(pick, "prediction")
        tier = _v(pick, "tier")
        model = _v(pick, "model_version").upper()
        color = TIER_COLORS.get(tier, DEFAULT_COLOR)

        ctx_parts = [p for p in [tier, model] if p and p != "?"]
        ctx_line = " | ".join(ctx_parts)

        embed = discord.Embed(
            title=f"{_v(pick, 'player_name')}: Line Cleared",
            description=(
                f"**{prediction} {line} {market}**\n"
                f"Currently at **{actual:.1f}**, line cleared mid-game\n"
                f"{ctx_line}"
            ),
            color=color,
        )
        embed.set_footer(text="Cephalon Lumen | Game-Time Copilot")
        return embed

    def result_embed(self, pick: dict) -> discord.Embed:
        """Hit/miss result embed."""
        is_hit = pick.get("is_hit", False)
        actual = pick.get("actual_value") or 0
        line = pick.get("line") or 0
        market = _v(pick, "market")
        tier = _v(pick, "tier")
        model = _v(pick, "model_version").upper()
        prediction = _v(pick, "prediction")

        if is_hit:
            title = f"HIT: {_v(pick, 'player_name')}"
            color = 0x22C55E
        else:
            title = f"MISS: {_v(pick, 'player_name')}"
            color = 0xEF4444

        ctx_parts = [p for p in [tier, model] if p and p != "?"]
        ctx_line = " | ".join(ctx_parts)

        embed = discord.Embed(
            title=title,
            description=(f"**{prediction} {line} {market}**\nFinal: **{actual:.1f}**\n{ctx_line}"),
            color=color,
        )

        rs = pick.get("rolling_stats")
        if rs and isinstance(rs, dict):
            stats_line = " | ".join(f"{k}: {v:.1f}" for k, v in rs.items() if v is not None)
            if stats_line:
                embed.add_field(name="Rolling Stats", value=stats_line, inline=False)

        embed.set_footer(text="Cephalon Lumen | Game-Time Copilot")
        return embed

    def correction_embed(self, pick: dict) -> discord.Embed:
        """A resolved prediction whose result changed when Lunara re-checked the final box score."""
        is_hit = pick.get("is_hit")
        line = pick.get("line") or 0
        market = _v(pick, "market")
        tier = _v(pick, "tier")
        model = _v(pick, "model_version").upper()
        prediction = _v(pick, "prediction")
        was = "HIT" if pick["previous_is_hit"] else "MISS"
        now = "HIT" if is_hit else "MISS"

        ctx_parts = [p for p in [tier, model] if p and p != "?"]
        ctx_line = " | ".join(ctx_parts)

        embed = discord.Embed(
            title=f"CORRECTED: {_v(pick, 'player_name')}",
            description=(
                f"**{prediction} {line} {market}**\n"
                f"Final: {pick['previous_actual']:.1f} \u2192 **{pick['actual_value']:.1f}**\n"
                f"Result: {was} \u2192 **{now}**\n"
                f"Lunara re-checked the final box score.\n{ctx_line}"
            ),
            color=0x22C55E if is_hit else 0xEF4444,
        )
        embed.set_footer(text="Cephalon Lumen | Game-Time Copilot")
        return embed

    def daily_recap_embed(self, picks: list[dict]) -> discord.Embed:
        """End-of-day recap: how many of the day's predictions hit."""
        resolved = [p for p in picks if p.get("is_hit") is not None]
        hits = sum(1 for p in resolved if p["is_hit"])
        wr = (hits / len(resolved) * 100) if resolved else 0

        embed = discord.Embed(
            title="Lumen: Daily Recap",
            description=f"**{hits} of {len(resolved)} hit** ({wr:.0f}%)",
            color=RECAP_COLOR,
        )

        for pick in resolved:
            embed.add_field(
                name=f"{pick.get('player_name', '?')}: {'HIT' if pick['is_hit'] else 'MISS'}",
                value=(
                    f"{pick.get('prediction', '?')} {pick.get('line', '?')} "
                    f"{pick.get('market', '?')} \u2192 **{pick.get('actual_value', 0):.1f}**"
                ),
                inline=True,
            )

        embed.set_footer(text="Cephalon Lumen | Game-Time Copilot")
        return embed

    # ── New Copilot embeds (context-aware, driven by intelligence engine) ──

    def copilot_embed(
        self,
        alert_type: AlertType,
        message: str,
        game: GameState | None = None,
        ctx: PickContext | None = None,
    ) -> discord.Embed:
        """Build a rich copilot embed from an intelligence engine message."""
        color = ALERT_COLORS.get(alert_type, DEFAULT_COLOR)

        title_map = {
            AlertType.HALFTIME_REPORT: "Halftime Report",
            AlertType.QUARTER_SUMMARY: "Quarter Update",
            AlertType.BLOWOUT_WARNING: "Blowout Alert",
            AlertType.GARBAGE_TIME: "Garbage Time",
            AlertType.PACE_COMFORT: "Pace Check",
            AlertType.PACE_CONCERN: "Pace Warning",
            AlertType.LINE_CLEARED_EARLY: "Early Clear",
            AlertType.FOUL_TROUBLE: "Foul Trouble",
            AlertType.SCORING_RUN: "Hot Streak",
            AlertType.DROUGHT: "Cold Streak",
            AlertType.DAILY_RECAP: "Daily Recap",
            AlertType.NOT_PLAYING: "Not Playing",
        }

        title = title_map.get(alert_type, "Lumen Update")

        # Add game context to title if available
        if game and alert_type != AlertType.DAILY_RECAP:
            title += f" | {game.away_team} @ {game.home_team}"

        embed = discord.Embed(title=title, description=message, color=color)

        # Add pick context footer for single-pick alerts
        if ctx:
            tier_label = ctx.tier or "?"
            model_label = ctx.model_version.upper() if ctx.model_version else "?"
            parts = [p for p in [tier_label, model_label] if p and p != "?"]
            embed.set_footer(text=f"{'  |  '.join(parts)}  \u2022  Cephalon Lumen")
        else:
            embed.set_footer(text="Cephalon Lumen | Game-Time Copilot")

        return embed

    def halftime_report_embed(self, game: GameState) -> discord.Embed:
        """Rich halftime report card with comfort bars for each pick."""
        score_line = f"{game.away_team} {game.away_score} - {game.home_team} {game.home_score}"

        embed = discord.Embed(
            title=f"Halftime Report | {game.away_team} @ {game.home_team}",
            description=f"**{score_line}**",
            color=0x3B82F6,
        )

        picks = sorted(
            [c for c in game.picks.values() if c.is_hit is None],
            key=lambda c: (
                [
                    "crushing",
                    "comfortable",
                    "on_track",
                    "tight",
                    "behind",
                    "buried",
                    "unknown",
                ].index(c.comfort_level)
                if c.comfort_level
                in ["crushing", "comfortable", "on_track", "tight", "behind", "buried", "unknown"]
                else 99
            ),
        )

        for ctx in picks[:10]:
            pace = ctx.latest_pace
            market = ctx.market.lower()
            bar = _comfort_bar(ctx.comfort_level)

            if ctx.not_playing:
                embed.add_field(
                    name=f"{ctx.player_name}: {market}",
                    value=(
                        "not playing, no result expected\n"
                        f"{ctx.prediction} {ctx.line} | {ctx.tier}/{ctx.model_version.upper()}"
                    ),
                    inline=True,
                )
                continue

            # Build context line from box score
            bs_parts = []
            bs = ctx.box_score
            if bs.minutes > 0:
                bs_parts.append(f"{bs.minutes:.0f}min")
            if bs.fg_attempted > 0:
                pct = bs.fg_made / bs.fg_attempted * 100
                bs_parts.append(f"{pct:.0f}%FG")
            if bs.fouls >= 3:
                bs_parts.append(f"{bs.fouls}PF")
            bs_str = f" ({', '.join(bs_parts)})" if bs_parts else ""

            if pace is not None:
                value = (
                    f"{ctx.actual_value:.0f} at half \u2192 pace **{pace:.0f}**{bs_str}\n"
                    f"{bar} {ctx.comfort_level}\n"
                    f"{ctx.prediction} {ctx.line} | {ctx.tier}/{ctx.model_version.upper()}"
                )
            else:
                value = (
                    f"{ctx.actual_value:.0f} at half{bs_str}\n"
                    f"{ctx.prediction} {ctx.line} | {ctx.tier}/{ctx.model_version.upper()}"
                )

            embed.add_field(
                name=f"{ctx.player_name}: {market}",
                value=value,
                inline=True,
            )

        # Blowout warning
        if game.abs_diff >= 15:
            embed.add_field(
                name="Score Alert",
                value=f"{game.leading_team} up **{game.abs_diff}**: watch for reduced 2H minutes",
                inline=False,
            )

        embed.set_footer(text="Cephalon Lumen | Game-Time Copilot")
        return embed

    def daily_copilot_recap(self, picks: list[PickContext]) -> discord.Embed:
        """Rich daily recap from PickContext objects (not raw dicts)."""
        resolved = [p for p in picks if p.is_hit is not None]
        if not resolved:
            return discord.Embed(
                title="Lumen: Daily Recap",
                description="No resolved picks today.",
                color=0x6B7280,
            )

        hits = sum(1 for p in resolved if p.is_hit)
        wr = (hits / len(resolved) * 100) if resolved else 0

        embed = discord.Embed(
            title="Lumen: Daily Recap",
            description=f"**{hits} of {len(resolved)} hit** ({wr:.0f}%)",
            color=RECAP_COLOR,
        )

        for p in sorted(resolved, key=lambda x: (not x.is_hit, x.player_name)):
            market = p.market.lower()
            embed.add_field(
                name=f"{p.player_name}: {'HIT' if p.is_hit else 'MISS'}",
                value=(
                    f"{p.prediction} {p.line} {market} \u2192 **{p.actual_value:.0f}**\n"
                    f"{p.tier}/{p.model_version.upper()}"
                ),
                inline=True,
            )

        embed.set_footer(text="Cephalon Lumen | Game-Time Copilot")
        return embed
