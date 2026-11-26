#!/usr/bin/env python3
"""Cephalon Lumen: Game-Time Copilot.

Real-time NBA pick tracking with intelligent commentary:
  - Pace projections: "Jokic has 18 at the half, on pace for 36"
  - Blowout alerts: "DEN up 28 in Q3, Jokic might sit Q4"
  - Halftime report cards with comfort levels
  - Quarter summaries and daily recaps
  - Game logs: the record of each night, which the tests replay
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from importlib.util import find_spec
from pathlib import Path
from zoneinfo import ZoneInfo

import discord
import httpx
import yaml

from formatter import PickFormatter
from settings import resolve_lunara_urls, resolve_owner_id
from ws_listener import WSListener

log = logging.getLogger("lumen")

EASTERN = ZoneInfo("America/New_York")


def setup_logging() -> None:
    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(fmt)
    root.addHandler(handler)

    logging.getLogger("discord.gateway").setLevel(logging.WARNING)
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("websockets").setLevel(logging.WARNING)


class Lumen(discord.Client):
    def __init__(self, config: dict) -> None:
        intents = discord.Intents.default()
        super().__init__(intents=intents)

        self.config = config
        self.owner_id: int = config["discord"]["owner_id"]
        self.api_url, self.ws_url = resolve_lunara_urls(config, os.environ)
        self.alert_cfg: dict = config.get("alerts", {})
        self.start_time = datetime.now(timezone.utc)
        self.formatter = PickFormatter()
        self._ws_listener: WSListener | None = None
        self._listener_task: asyncio.Task | None = None
        self._brain = None  # the AI layer's brain (ai.make_brain), or None without it
        self._last_history_clear_date: str = ""

    async def on_ready(self) -> None:
        log.info("Lumen online: %s (ID: %s)", self.user, self.user.id)
        await self.change_presence(
            activity=discord.Activity(
                type=discord.ActivityType.watching,
                name="tracked picks live",
            )
        )

        # discord.py fires on_ready again whenever it opens a fresh gateway session.
        # The background tasks below must run once per process, not once per session.
        if self._listener_task is not None:
            log.info("Gateway session renewed; background tasks already running")
            return

        # Start the health endpoint
        asyncio.create_task(self._health_server())

        # Start WebSocket listener (the copilot brain)
        self._ws_listener = WSListener(
            api_url=self.api_url,
            ws_url=self.ws_url,
            alert_cfg=self.alert_cfg,
            send_dm=self._send_dm,
            formatter=self.formatter,
        )
        self._listener_task = asyncio.create_task(self._ws_listener.run())
        log.info("Game-Time Copilot started")

        # Initialize AI brain
        self._init_brain()

        # Atlas fleet heartbeat
        if os.environ.get("ATLAS_HEARTBEAT_SECRET"):
            asyncio.create_task(self._atlas_heartbeat())

    def _init_brain(self) -> None:
        """Lumen's brain for conversational DMs, from the AI layer (ai.py) when it's installed.
        Without it Lumen runs on its rules alone: alerts, status, picks and recap."""
        if find_spec("ai") is None:
            log.info("AI layer not installed: DMs answer status, picks and recap only")
            self._brain = None
            return
        import ai  # a broken AI layer fails loudly here, as a missing import always has

        self._brain = ai.make_brain(self)
        log.info("AI brain: %s", "ONLINE" if self._brain.available else "OFFLINE")

    async def on_message(self, message: discord.Message) -> None:
        """Handle DM commands and AI conversations from the owner."""
        if message.author.id != self.owner_id:
            return
        if not isinstance(message.channel, discord.DMChannel):
            return

        content = message.content.strip()
        content_lower = content.lower()

        # Explicit commands (quick, no AI needed)
        if content_lower in ("status", "!status"):
            await self._send_status(message.channel)
            return
        if content_lower in ("picks", "!picks"):
            await self._send_current_picks(message.channel)
            return
        if content_lower in ("recap", "!recap"):
            await self._send_recap(message.channel)
            return

        # Everything else goes to the AI brain
        if self._brain and self._brain.available:
            # Auto-clear history at day boundary (Eastern calendar day, not UTC).
            today = datetime.now(EASTERN).strftime("%Y-%m-%d")
            if self._last_history_clear_date != today:
                self._brain.clear_history(message.author.id)
                self._last_history_clear_date = today

            async with message.channel.typing():
                reply = await self._brain.respond(
                    user_id=message.author.id,
                    message=content,
                    user_name=message.author.name,
                )

            for chunk in _split_message(reply, 2000):
                await message.reply(chunk)

    async def _send_status(self, channel: discord.DMChannel) -> None:
        """Send current copilot status."""
        uptime = int((datetime.now(timezone.utc) - self.start_time).total_seconds())
        hours, remainder = divmod(uptime, 3600)
        minutes, seconds = divmod(remainder, 60)

        listener = self._ws_listener
        active_games = len(listener._game_tasks) if listener else 0
        total_picks = sum(len(g.picks) for g in listener.engine.games.values()) if listener else 0
        resolved = len(listener.engine._resolved_pick_ids) if listener else 0

        embed = discord.Embed(
            title="Lumen Status",
            color=0x3B82F6,
            description=(
                f"**Uptime:** {hours}h {minutes}m {seconds}s\n"
                f"**Active Games:** {active_games}\n"
                f"**Tracking:** {total_picks} picks ({resolved} resolved)\n"
                f"**Engine:** Game Context + Intelligence active"
            ),
        )
        embed.set_footer(text="Cephalon Lumen | Game-Time Copilot")
        await channel.send(embed=embed)

    async def _send_current_picks(self, channel: discord.DMChannel) -> None:
        """Send current pick status for all active games."""
        if not self._ws_listener:
            return

        for game in self._ws_listener.engine.games.values():
            if not game.picks or game.status == "scheduled":
                continue

            score_line = f"{game.away_team} {game.away_score} - {game.home_team} {game.home_score}"
            status = game.status.upper()
            if game.quarter and game.clock:
                ql = f"Q{game.quarter}" if game.quarter <= 4 else f"OT{game.quarter - 4}"
                status = f"{ql} {game.clock}"

            embed = discord.Embed(
                title=f"{game.away_team} @ {game.home_team}",
                description=f"**{score_line}** | {status}",
                color=0x3B82F6,
            )

            for ctx in game.picks.values():
                pace = ctx.latest_pace
                pace_str = f" \u2192 pace {pace:.0f}" if pace else ""
                market = ctx.market.lower()
                comfort = ctx.comfort_level

                if ctx.is_hit is not None:
                    result = "HIT" if ctx.is_hit else "MISS"
                    value = f"{ctx.actual_value:.0f} {market} \u2192 **{result}**"
                else:
                    value = (
                        f"{ctx.actual_value:.0f} {market}{pace_str}\n"
                        f"{ctx.prediction} {ctx.line} | {comfort}"
                    )

                embed.add_field(
                    name=ctx.player_name,
                    value=value,
                    inline=True,
                )

            embed.set_footer(text="Cephalon Lumen | Game-Time Copilot")
            await channel.send(embed=embed)

    async def _send_recap(self, channel: discord.DMChannel) -> None:
        """Send daily recap on demand."""
        if not self._ws_listener:
            return

        resolved = self._ws_listener.engine.get_all_resolved_today()
        if not resolved:
            await channel.send("No resolved picks yet today.")
            return

        embed = self._ws_listener.formatter.daily_copilot_recap(resolved)
        await channel.send(embed=embed)

    async def _send_dm(self, content: str | None = None, embed: discord.Embed | None = None):
        """Send a DM to the owner."""
        try:
            user = await self.fetch_user(self.owner_id)
            if user:
                await user.send(content=content, embed=embed)
        except discord.Forbidden:
            log.warning("Cannot DM owner; DMs may be disabled")
        except Exception:
            log.exception("Failed to send DM")

    async def _health_server(self) -> None:
        """Minimal HTTP server so a supervisor can check he's alive."""
        port = int(os.environ.get("PORT", "8080"))
        host = os.environ.get("HEALTH_HOST", "127.0.0.1")

        async def handle(reader, writer):
            await reader.read(1024)

            body = '{"status": "ok", "service": "cephalon-lumen"}'

            writer.write(
                f"HTTP/1.1 200 OK\r\n"
                f"Content-Type: application/json\r\n"
                f"Content-Length: {len(body)}\r\n\r\n{body}".encode()
            )
            await writer.drain()
            writer.close()

        server = await asyncio.start_server(handle, host, port)
        log.info("Health server listening on %s:%d", host, port)
        async with server:
            await server.serve_forever()

    async def _atlas_heartbeat(self) -> None:
        """Send periodic heartbeat to Cephalon Atlas fleet monitor."""
        await self.wait_until_ready()
        secret = os.environ.get("ATLAS_HEARTBEAT_SECRET", "")
        url = os.environ.get("ATLAS_HEARTBEAT_URL", "")
        if not url:
            log.info("Atlas heartbeat off: ATLAS_HEARTBEAT_URL is not set")
            return
        log.info("Atlas heartbeat sender started")

        while not self.is_closed():
            try:
                uptime = int((datetime.now(timezone.utc) - self.start_time).total_seconds())
                listener = self._ws_listener
                active_games = len(listener._game_tasks) if listener else 0
                tracked_picks = (
                    sum(len(g.picks) for g in listener.engine.games.values()) if listener else 0
                )

                payload = {
                    "secret": secret,
                    "bot": "lumen",
                    "status": "ok",
                    "uptime": uptime,
                    "active_games": active_games,
                    "tracked_picks": tracked_picks,
                    "engine": "copilot_v2",
                }
                async with httpx.AsyncClient() as client:
                    await client.post(url, json=payload, timeout=5.0)
            except Exception:
                pass
            await asyncio.sleep(60)

    async def close(self) -> None:
        if self._listener_task:
            self._listener_task.cancel()
        if self._ws_listener:
            await self._ws_listener.stop()
        await super().close()


def _split_message(text: str, limit: int = 2000) -> list[str]:
    """Split a message into chunks that fit Discord's character limit."""
    if len(text) <= limit:
        return [text]
    chunks = []
    while text:
        if len(text) <= limit:
            chunks.append(text)
            break
        # Try to split at a newline
        idx = text.rfind("\n", 0, limit)
        if idx == -1:
            idx = limit
        chunks.append(text[:idx])
        text = text[idx:].lstrip("\n")
    return chunks


def main() -> None:
    setup_logging()

    config_path = Path(__file__).parent / "config.yaml"
    with open(config_path) as f:
        config = yaml.safe_load(f)

    token = os.environ.get("DISCORD_TOKEN", config.get("discord", {}).get("token", ""))
    if not token:
        log.error("DISCORD_TOKEN env var not set")
        sys.exit(1)
    owner_id = resolve_owner_id(os.environ)
    if owner_id is None:
        log.error("LUMEN_OWNER_ID env var not set (the owner's Discord id)")
        sys.exit(1)
    config.setdefault("discord", {})["owner_id"] = owner_id

    bot = Lumen(config)
    bot.run(token, log_handler=None)


if __name__ == "__main__":
    main()
