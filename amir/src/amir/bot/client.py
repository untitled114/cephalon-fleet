"""AmirBot: the Discord client that wires every service into one process.

Every channel write goes through the one GatedWriter that `build_services` creates around the
DiscordSender; no other object holds the sender. While publishing is off the gate keeps no
reference to it at all.

Lifecycle: setup_hook reads back a voice hold left by an earlier run, before any gateway event,
then registers and syncs the guild's commands (a failed sync is logged, never fatal). The first
on_ready settles interrupted posts, scans the greeting clips and starts each background task
once; a gateway reconnect fires on_ready again and changes nothing. close() stops the
companion's follow task and hold-save retry and then the tasks, removes the heartbeat, leaves
voice (bounded), closes the gateway, then the HTTP session, then the database. Voice updates are
ignored from then on.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import aiohttp
import discord
from discord import app_commands

from amir.baro.service import BaroService
from amir.bot.tasks import TaskFactory, TaskSupervisor
from amir.clock import Clock, SystemClock
from amir.config.settings import Settings
from amir.cycles.service import CycleService
from amir.health import write_heartbeat
from amir.news.pipeline import Fetcher, NewsPipeline
from amir.publishing.discord_sender import DiscordSender
from amir.publishing.gate import ChannelKey, GatedWriter
from amir.publishing.outbox import OutboxWorker
from amir.storage.db import Database
from amir.storage.repos import (
    BaroVisitRepo,
    CycleCardRepo,
    ItemNameRepo,
    NewsItemRepo,
    OutboxRepo,
    PreviewRepo,
    SourceStateRepo,
    VoiceHoldRepo,
)
from amir.voice.announcer import VoiceAnnouncer
from amir.voice.classify import VoiceEventKind, classify
from amir.voice.companion import VoiceCompanion, VoiceEvent
from amir.voice.discord_adapter import (
    DiscordKickDetector,
    DiscordPlayer,
    DiscordVoiceConnector,
    read_census,
)
from amir.voice.follow import VoiceCensus
from amir.voice.greetings import GreetingLibrary
from amir.voice.queue import GreetingQueue
from amir.voice.rejoin import RejoinTracker
from amir.voice.session import VoiceSession
from amir.voice.stay import StayTracker
from amir.worldstate.poller import WorldStatePoller

log = logging.getLogger(__name__)

CYCLES_TICK_SECONDS = 1
CYCLES_ENSURE_SECONDS = 30
HEARTBEAT_SECONDS = 30
VOICE_DISCONNECT_TIMEOUT = 10.0

RegisterCommands = Callable[["AmirBot"], Awaitable[None]]
VocalChannel = discord.VoiceChannel | discord.StageChannel


def build_intents() -> discord.Intents:
    """Guilds and voice states only: no privileged intent."""
    intents = discord.Intents.none()
    intents.guilds = True
    intents.voice_states = True
    return intents


def _channel_id(channel: VocalChannel | None) -> int | None:
    return None if channel is None else channel.id


def _seen_channel_id(channel: VocalChannel | None) -> int | None:
    """`channel`'s id, or None for no channel or for one Amir's role can't view. Discord may
    still send voice states for a channel hidden from him; who comes and goes there is not his
    to see."""
    if channel is None:
        return None
    me = channel.guild.me
    if me is None or not channel.permissions_for(me).view_channel:
        return None
    return channel.id


@dataclass
class Services:
    settings: Settings
    db: Database
    http: aiohttp.ClientSession
    fetcher: Fetcher
    writer: GatedWriter
    outbox: OutboxWorker
    news: NewsPipeline
    worldstate: WorldStatePoller
    cycles: CycleService
    companion: VoiceCompanion
    announcer: VoiceAnnouncer
    greetings: GreetingQueue
    library: GreetingLibrary
    session: VoiceSession
    supervisor: TaskSupervisor
    clock: Clock


def build_services(
    client: AmirBot,
    settings: Settings,
    db: Database,
    http: aiohttp.ClientSession,
    fetcher: Fetcher,
) -> Services:
    """Wire every service. The DiscordSender exists only inside the one GatedWriter."""
    clock = SystemClock()
    monotonic = time.monotonic  # the greeting queue, the stays and the companion share one clock
    ids = settings.discord
    voice = settings.voice

    writer = GatedWriter(
        DiscordSender(
            client,
            {
                ChannelKey.NEWS: ids.news_channel_id,
                ChannelKey.CYCLES: ids.cycles_channel_id,
                ChannelKey.VOICE_LOG: ids.voice_log_channel_id,
            },
        ),
        PreviewRepo(db, clock),
        enabled=settings.publishing.enabled,
    )
    outbox_repo = OutboxRepo(db, clock)
    news = NewsPipeline(
        fetcher,
        SourceStateRepo(db, clock),
        NewsItemRepo(db, clock),
        outbox_repo,
        settings.news,
        clock,
        notification_role_id=ids.notification_role_id,
    )
    baro = BaroService(
        outbox_repo,
        BaroVisitRepo(db),
        writer,
        settings.baro,
        clock,
        notification_role_id=ids.notification_role_id,
        role_ping_categories=settings.news.role_ping_categories,
    )
    cycles = CycleService(
        writer,
        CycleCardRepo(db),
        worlds=settings.cycles.worlds,
        durations=settings.cycles.durations,
        clock=clock,
    )
    connector = DiscordVoiceConnector(client)
    session = VoiceSession(
        connector,
        grace_seconds=voice.leave_grace_seconds,
        connect_timeout=voice.connect_timeout_seconds,
        max_attempts=voice.reconnect_max_attempts,
    )
    library = GreetingLibrary(Path(voice.greetings_dir), voice.ffmpeg_path)
    announcer = VoiceAnnouncer(writer)
    greetings = GreetingQueue(
        capacity=voice.queue_capacity,
        expiry_seconds=voice.greeting_expiry_seconds,
        clock=monotonic,
    )
    companion = VoiceCompanion(
        home_channel_id=ids.voice_channel_id,
        session=session,
        queue=greetings,
        library=library,
        player=DiscordPlayer(connector.voice_client, voice.ffmpeg_path, voice.audio_filter),
        announcer=announcer,
        stays=StayTracker(
            voice.greeting_expiry_seconds,
            RejoinTracker(voice.rejoin_window_seconds, clock=monotonic),
            greet_on_rejoin=voice.greet_on_rejoin,
            clock=monotonic,
        ),
        settings=voice,
        census=client.voice_census,
        settle_seconds=voice.follow_settle_seconds,
        kicks=DiscordKickDetector(client, ids.guild_id, clock),
        holds=VoiceHoldRepo(db),
        wall_clock=clock,
        clock=monotonic,
    )
    return Services(
        settings=settings,
        db=db,
        http=http,
        fetcher=fetcher,
        writer=writer,
        outbox=OutboxWorker(outbox_repo, writer, clock),
        news=news,
        worldstate=WorldStatePoller(
            fetcher, settings.worldstate, baro, cycles, ItemNameRepo(db), clock
        ),
        cycles=cycles,
        companion=companion,
        announcer=announcer,
        greetings=greetings,
        library=library,
        session=session,
        supervisor=TaskSupervisor(),
        clock=clock,
    )


class AmirBot(discord.Client):
    """`services` builds the bot's services from the bot itself: the DiscordSender, the voice
    connector and the voice census all need this client. `register_commands` adds the slash
    commands to `tree` (None: no commands). `voice_census` reads who is in each voice channel
    (None: discord.py's cache)."""

    def __init__(
        self,
        settings: Settings,
        services: Callable[[AmirBot], Services],
        *,
        register_commands: RegisterCommands | None = None,
        voice_census: Callable[[], VoiceCensus] | None = None,
    ) -> None:
        super().__init__(intents=build_intents())
        self.settings = settings
        self.tree = app_commands.CommandTree(self)
        self._register_commands = register_commands
        self._voice_census = voice_census or self._cached_voice_census
        self._ready_once = False
        self._closing = False
        self._closed = asyncio.Event()
        # Held by a heartbeat write and by its removal at shutdown (both in worker threads).
        self._heartbeat_lock = threading.Lock()
        self.services = services(self)

    # --- lifecycle -----------------------------------------------------------------------------

    async def setup_hook(self) -> None:
        # Before the gateway connects: no voice event may find a moderator's hold unread.
        await self.services.companion.load_hold()
        if self._register_commands is not None:
            await self._register_commands(self)
        try:
            await self.tree.sync(guild=discord.Object(id=self.settings.discord.guild_id))
        except discord.HTTPException as exc:
            log.error("slash command sync failed (HTTP %s); running without it", exc.status)
        else:
            log.info("slash commands synced")

    async def on_ready(self) -> None:
        if self._ready_once:
            log.info("discord ready again after a reconnect; background tasks already run")
            return
        self._ready_once = True  # before any await: a second on_ready must not start anything
        log.info("discord ready")
        s = self.services
        try:
            await s.outbox.reconcile_inflight()
        except Exception:
            log.exception("outbox reconciliation failed; interrupted posts stay unsettled")
        try:
            report = await s.library.scan()
        except Exception:
            log.exception("greeting clip scan failed")
        else:
            # Counts only: clip names and paths stay out of this line.
            log.info(
                "greetings scanned: %d valid, %d invalid", len(report.valid), len(report.invalid)
            )
        workers: dict[str, TaskFactory] = {
            "outbox": s.outbox.run,
            "news": s.news.run,
            "worldstate": s.worldstate.run,
            "cycles-ticker": self._cycles_ticker,
            "cycles-ensure": self._cycles_ensure,
            "announcer": s.announcer.run,
            "greetings": lambda: s.greetings.run(s.companion.play_one),
            "heartbeat": self._heartbeat,
        }
        for name, factory in workers.items():
            s.supervisor.start(name, factory)
        # Never awaited here: voice backoff must not delay the workers or announcements.
        s.supervisor.start("voice-startup", s.companion.on_startup)

    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        if self._closing:
            return  # shutting down: nothing may start a greeting, a line or a rejoin
        # Read before any await: a deliberate leave sets it only while it runs.
        expecting_disconnect = self.services.session.expecting_disconnect
        own_id = self.user.id if self.user is not None else 0
        if member.id == own_id:
            self._own_voice_update(
                _channel_id(before.channel), _channel_id(after.channel), expecting_disconnect
            )
            return
        if before.channel is not None and after.channel is None:
            # Any disconnect, even in a channel hidden from Amir: a moderator's adds to the
            # audit log all the same.
            self._member_left_voice()
        # Members' privacy: a channel hidden from Amir's role is no channel to him, so a move
        # from or to it is a join or a leave, and anything inside it is nothing.
        before_id = _seen_channel_id(before.channel)
        after_id = _seen_channel_id(after.channel)
        kind = classify(
            member_id=member.id,
            is_bot=member.bot,
            own_id=own_id,
            before_channel_id=before_id,
            after_channel_id=after_id,
        )
        if kind is None:
            return
        # The channel joined, left, or moved to; a move also carries the one it came from.
        channel_id = before_id if kind is VoiceEventKind.LEAVE else after_id
        from_id = before_id if kind is VoiceEventKind.MOVE else None
        assert channel_id is not None  # classify() only returns a kind when there is one
        # on_event never suspends: it queues the line and any greeting, and wakes the follow
        # task, which decides where Amir sits off the event path.
        await self.services.companion.on_event(VoiceEvent(kind, member.id, channel_id, from_id))

    def _own_voice_update(
        self, before_id: int | None, after_id: int | None, expecting_disconnect: bool
    ) -> None:
        """Amir's own voice updates, in any channel, each handled off the event path. He never
        moves himself from one channel to another: he leaves (expecting_disconnect is True
        meanwhile), then joins. So a change straight from one channel to another is a drag."""
        s = self.services
        if before_id is not None and after_id is None:
            if not expecting_disconnect:
                # Disconnected: a moderator's kick holds him out; after anything else he
                # follows the crowd again.
                s.supervisor.start("voice-rejoin", s.companion.on_own_disconnect)
        elif before_id is not None and after_id is not None and before_id != after_id:
            # Dragged to another channel: deliberate, so he stays there with them (a pin).
            s.supervisor.start(
                "voice-move", lambda: s.companion.on_own_move(after_id), routine=True
            )
        elif before_id is None and after_id is not None:
            # Joined: note the audit log now, to tell a later kick from an accident. discord.py
            # reports it before the voice handshake is done; the follow decision that connected
            # him greets whoever there just arrived in voice once it is.
            s.supervisor.start(
                "voice-snapshot", lambda: s.companion.on_own_connect(after_id), routine=True
            )

    def _member_left_voice(self) -> None:
        """Another member (anywhere in the guild) left voice, perhaps disconnected by a
        moderator. While Amir is in voice or connecting (every follow move is a connect), the
        audit log is read again a little later, so that entry is never taken for a kick of
        Amir."""
        s = self.services
        if s.session.state in ("connecting", "connected", "grace"):
            s.companion.note_member_disconnect()
            s.supervisor.start("voice-audit-refresh", s.companion.refresh_snapshot, routine=True)

    async def close(self) -> None:
        """Shut down once; a concurrent or later call waits for that shutdown to finish."""
        if self._closing:
            await self._closed.wait()
            return
        self._closing = True
        try:
            await self._shut_down()
        finally:
            self._closed.set()

    async def _shut_down(self) -> None:
        s = self.services
        log.info("shutting down")
        try:
            # First: the follow task would hold the session lock, then connect. And before the
            # tasks: cancelling a pending kick check releases a follow waiting on it. This also
            # stops a hold-save retry; the database closes below.
            await s.companion.cancel_background()
        except Exception:
            log.exception("stopping the companion's background tasks failed")
        try:
            await s.supervisor.cancel_all()
        except Exception:
            log.exception("stopping the background tasks failed")
        try:
            # The heartbeat task is stopped; without the file the health check fails at once.
            heartbeat = Path(self.settings.logging.heartbeat_path)
            await asyncio.to_thread(self._remove_heartbeat, heartbeat)
        except Exception:
            log.exception("removing the heartbeat failed")
        try:
            await asyncio.wait_for(s.session.disconnect(), VOICE_DISCONNECT_TIMEOUT)
        except TimeoutError:
            log.warning("leaving voice took over %ss; closing anyway", VOICE_DISCONNECT_TIMEOUT)
        except Exception:
            log.exception("leaving voice failed")
        try:
            await super().close()
        except Exception:
            log.exception("closing the Discord connection failed")
        try:
            await s.http.close()
        except Exception:
            log.exception("closing the HTTP session failed")
        try:
            await s.db.close()
        except Exception:
            log.exception("closing the database failed")
        log.info("shut down")

    # --- presence ------------------------------------------------------------------------------

    def voice_census(self) -> VoiceCensus:
        """Who is in each voice channel of the guild."""
        return self._voice_census()

    def _cached_voice_census(self) -> VoiceCensus:
        own_id = self.user.id if self.user is not None else 0
        return read_census(
            self.get_guild(self.settings.discord.guild_id),
            own_id,
            self.settings.voice.exclude_channel_ids,
        )

    # --- background loops ----------------------------------------------------------------------

    async def _cycles_ticker(self) -> None:
        while True:
            await self.services.cycles.tick()
            await asyncio.sleep(CYCLES_TICK_SECONDS)

    async def _cycles_ensure(self) -> None:
        while True:
            await self.services.cycles.ensure_cards()
            await asyncio.sleep(CYCLES_ENSURE_SECONDS)

    async def _heartbeat(self) -> None:
        path = Path(self.settings.logging.heartbeat_path)
        while True:
            if self.is_ready() and not self.is_closed():
                await asyncio.to_thread(self._write_heartbeat, path)
            await asyncio.sleep(HEARTBEAT_SECONDS)

    # Cancelling the heartbeat task cannot stop a write already in its thread. Write and removal
    # take turns on one lock, and a write checks the closing flag first, so a write either ends
    # before shutdown removes the file or never starts: the file is never recreated after it.

    def _write_heartbeat(self, path: Path) -> None:
        with self._heartbeat_lock:
            if not self._closing:
                write_heartbeat(path, datetime.now(UTC))

    def _remove_heartbeat(self, path: Path) -> None:
        with self._heartbeat_lock:
            path.unlink(missing_ok=True)
