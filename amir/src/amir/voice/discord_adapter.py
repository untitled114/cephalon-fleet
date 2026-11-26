"""discord.py voice adapter: the VoiceConnector, Player and KickDetector ports.

Amir joins self-deafened, so he never receives audio. FFmpeg encodes Opus itself, so no
Python-side Opus encoder is involved in playback.
"""

import asyncio
import contextlib
import logging
from collections.abc import Callable, Collection
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

import aiohttp
import discord
from discord.voice_state import VoiceConnectionState

from amir.clock import Clock
from amir.voice.follow import ChannelView, VoiceCensus
from amir.voice.session import PlaybackError, VoiceConfigError

log = logging.getLogger(__name__)

AUDIT_LOG_LIMIT = 10
KICK_WINDOW_SECONDS = 60
AUDIT_LOG_UNREADABLE = (
    "can't read the audit log; grant View Audit Log so a moderator's kick keeps Amir out"
)
# The once-a-process warnings for a read that fails for another reason than the permission:
# Discord answered with an error, or did not answer at all. Their prefixes are their own, so they
# are easy to find among the per-failure lines.
_UNTIL_READABLE = (
    "until it can be read again, a disconnect counts as accidental, and a stale snapshot can"
    " later mistake an accident for a kick"
)
AUDIT_LOG_FAILED = "audit log unavailable ({}): " + _UNTIL_READABLE
AUDIT_LOG_UNREACHABLE = "audit log unreachable ({}): " + _UNTIL_READABLE
HEAR_WAIT_RAN_OUT = (
    "greeting starts before the listener has joined the call's encryption; its start may be cut"
)
HEAR_POLL_SECONDS = 0.05
# Discord's voice server sometimes closes a brand-new session with 4006 ("session no longer
# valid") when Amir identifies about 60 ms after Discord handed him the session: the server is
# close to Discord's voice servers, and the session has not reached the voice server yet. A
# short pause before a new session's websocket opens gives it that time.
VOICE_CONNECT_PAUSE_SECONDS = 0.5


def can_hear_amir(vc: discord.VoiceClient, listener_id: int | None) -> bool:
    """True once `listener_id` can decode what Amir sends.

    Discord encrypts voice end to end (DAVE) and re-keys the call whenever someone joins. Audio
    sent before Amir's session is ready, before the listener is in the group, or while a key
    change is pending can't be decoded by the listener's client, which drops it: the greeting's
    start goes missing. Reads discord.py 2.7.1's VoiceConnectionState; a call without end-to-end
    encryption is heard at once.

    With no listener (the test greeting greets nobody in particular) there is no group member to
    wait for: True once Amir's session is ready and no key change is pending.
    """
    state = getattr(vc, "_connection", None)
    if state is None or getattr(state, "dave_protocol_version", 0) == 0:
        return True
    session = state.dave_session
    if session is None or not session.ready or state.dave_pending_transitions:
        return False
    if listener_id is None:
        return True
    return str(listener_id) in {str(user_id) for user_id in session.get_user_ids()}


def build_source(path: Path, ffmpeg: str, audio_filter: str) -> discord.FFmpegOpusAudio:
    return discord.FFmpegOpusAudio(
        str(path),
        executable=ffmpeg,
        before_options="-nostdin",
        options=f"-vn -af {audio_filter}",
    )


class PausingConnectionState(VoiceConnectionState):
    """discord.py 2.7.1's voice connection state, pausing before a new session identifies.

    `_connect_websocket` opens the voice websocket and identifies; a resume reuses a session the
    voice server already knows, so only a new session waits.
    """

    sleep = staticmethod(asyncio.sleep)

    async def _connect_websocket(self, resume: bool) -> Any:
        if not resume:
            await self.sleep(VOICE_CONNECT_PAUSE_SECONDS)
        return await super()._connect_websocket(resume)


class AmirVoiceClient(discord.VoiceClient):
    """discord.VoiceClient with the pausing connection state."""

    def create_connection_state(self) -> VoiceConnectionState:
        return PausingConnectionState(self)


class DiscordVoiceConnector:
    """Connects the bot to whichever voice channel it is asked for; connected means connected
    to voice, in any channel of the guild.

    disconnect() leaves voice wherever Amir is, including a channel he was dragged to. Amir
    never moves himself between channels: discord.py's move_to sends a voice state without the
    deafen flag, which would undeafen him. Going elsewhere is a leave, then a self-deafened join.
    """

    def __init__(self, client: discord.Client) -> None:
        self._client = client
        self._guild: discord.Guild | None = None

    async def connect(
        self,
        channel_id: int,
        timeout: float,  # noqa: ASYNC109 - the VoiceConnector protocol takes a timeout
    ) -> None:
        channel = await self._resolve_channel(channel_id)
        self._guild = channel.guild
        if self.current_channel_id() == channel_id:
            return  # already there
        current = self._guild_voice_client()
        if current is not None:
            # In voice in another channel of this guild (the session leaves first, so a failed
            # leave or a drag racing this connect), or registered but not connected, e.g. left
            # behind by a cancelled connect (discord.py soft-disconnects and keeps it
            # registered). Leave or clear it: never a move, never a second connection.
            await current.disconnect(force=True)
        await channel.connect(timeout=timeout, reconnect=True, self_deaf=True, cls=AmirVoiceClient)

    async def disconnect(self) -> bool:
        """Leave voice in whatever channel Amir is in. True if he was connected to voice.

        A registered but unconnected voice client is cleared as well, and reports False.
        """
        current = self._guild_voice_client()
        if current is None:
            return False
        was_connected = current.is_connected()
        await current.disconnect(force=True)
        return was_connected

    def is_connected(self) -> bool:
        return self.voice_client() is not None

    def current_channel_id(self) -> int | None:
        """The voice channel Amir is connected to, or None."""
        current = self.voice_client()
        if current is None or current.channel is None:
            return None
        return current.channel.id

    def voice_client(self) -> discord.VoiceClient | None:
        """The guild's voice client while it is connected, in any channel, else None."""
        current = self._guild_voice_client()
        if current is None or not current.is_connected():
            return None
        return current

    def _guild_voice_client(self) -> discord.VoiceClient | None:
        if self._guild is None:
            return None
        return cast(discord.VoiceClient | None, self._guild.voice_client)

    async def _resolve_channel(self, channel_id: int) -> discord.VoiceChannel:
        channel = self._client.get_channel(channel_id)
        if channel is None:
            channel = await self._client.fetch_channel(channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            raise VoiceConfigError(
                f"channel {channel_id} is a {type(channel).__name__}, not a voice channel"
            )
        return channel


class DiscordPlayer:
    """Plays one clip to the end over the current voice client; never interrupts another."""

    def __init__(
        self,
        voice_client_provider: Callable[[], discord.VoiceClient | None],
        ffmpeg: str,
        audio_filter: str,
        timeout: float = 60.0,
        hear_wait: float = 5.0,
    ) -> None:
        self._voice_client_provider = voice_client_provider
        self._ffmpeg = ffmpeg
        self._audio_filter = audio_filter
        self._timeout = timeout
        self._hear_wait = hear_wait

    async def play(self, path: Path, listener_id: int | None) -> None:
        """Play `path` once `listener_id` can hear it, waiting at most `hear_wait` seconds. With
        no listener (a test greeting), only Amir's own session and key changes are waited for."""
        vc = self._connected_client()
        await self._until_heard(vc, listener_id)
        vc = self._connected_client()

        loop = asyncio.get_running_loop()
        finished: asyncio.Future[Exception | None] = loop.create_future()

        def resolve(error: Exception | None) -> None:
            if not finished.done():
                finished.set_result(error)

        def after(error: Exception | None) -> None:
            # Runs in discord.py's audio player thread, never on the event loop.
            with contextlib.suppress(RuntimeError):  # the loop is already closed at shutdown
                loop.call_soon_threadsafe(resolve, error)

        try:
            source = build_source(path, self._ffmpeg, self._audio_filter)
        except Exception as exc:
            raise PlaybackError(f"could not start FFmpeg for {path.name}") from exc
        try:
            vc.play(source, after=after)
        except Exception as exc:
            source.cleanup()
            raise PlaybackError(f"voice client refused to play {path.name}") from exc

        try:
            error = await asyncio.wait_for(finished, timeout=self._timeout)
        except TimeoutError:
            vc.stop()
            raise PlaybackError(f"{path.name} did not finish within {self._timeout:g} s") from None
        except asyncio.CancelledError:
            vc.stop()
            raise
        if error is not None:
            # The audio thread reports source and decoding errors here: the clip's fault.
            raise PlaybackError(f"playback of {path.name} failed", clip_fault=True) from error

    def _connected_client(self) -> discord.VoiceClient:
        vc = self._voice_client_provider()
        if vc is None or not vc.is_connected():
            raise PlaybackError("not connected to voice")
        if vc.is_playing():
            raise PlaybackError("already playing; a greeting never interrupts another")
        return vc

    async def _until_heard(self, vc: discord.VoiceClient, listener_id: int | None) -> None:
        if can_hear_amir(vc, listener_id):
            return
        loop = asyncio.get_running_loop()
        started = loop.time()
        while not can_hear_amir(vc, listener_id):
            if loop.time() - started >= self._hear_wait:
                log.warning(HEAR_WAIT_RAN_OUT)
                return
            if not vc.is_connected():
                return  # play() reports it
            await asyncio.sleep(HEAR_POLL_SECONDS)
        waited_ms = round((loop.time() - started) * 1000)
        log.info("greeting waited %d ms for the call's encryption", waited_ms)


class DiscordKickDetector:
    """KickDetector over the guild's audit log.

    Discord's member_disconnect entries name no target, and a moderator's disconnects are
    aggregated into one entry: a later one raises its count and keeps its created_at. So a kick
    of Amir shows as an entry that is new since the snapshot and less than 60 s old, or as a
    higher count on an entry the snapshot holds. Entries made by bots never count. Nothing about
    an entry (who, whom, how many) is ever logged. Reading the audit log needs View Audit Log;
    without it every disconnect counts as accidental, with one warning per process. A read that
    fails otherwise (Discord answers with an error, or the network doesn't answer) is handled
    the same way, with a warning of its own kind, once per process. Neither method raises.
    """

    def __init__(self, client: discord.Client, guild_id: int, clock: Clock) -> None:
        self._client = client
        self._guild_id = guild_id
        self._clock = clock
        self._counts: dict[int, int] = {}  # entry id -> count, as of the last snapshot
        self._warned: set[str] = set()  # the kinds of failure already warned about

    async def snapshot(self, keep: Callable[[], bool] | None = None) -> None:
        entries = await self._read()
        if entries is None:  # a failed read keeps the last snapshot
            return
        if keep is not None and not keep():  # asked after the read, with no await between
            return
        self._counts = {entry_id: count for entry_id, (count, _) in entries.items()}

    async def was_kicked(self) -> bool:
        entries = await self._read()
        if entries is None:
            return False
        recent = self._clock.now() - timedelta(seconds=KICK_WINDOW_SECONDS)
        for entry_id, (count, created_at) in entries.items():
            before = self._counts.get(entry_id)
            if before is None and created_at >= recent:
                return True
            if before is not None and count > before:
                return True
        return False

    async def _read(self) -> dict[int, tuple[int, datetime]] | None:
        """The latest member_disconnect entries by non-bots: id -> (count, created_at). None
        when the audit log can't be read."""
        guild = self._client.get_guild(self._guild_id)
        if guild is None:
            log.debug("audit log not read: the guild is not available yet")
            return None
        try:
            return {
                entry.id: (_count(entry), entry.created_at)
                async for entry in guild.audit_logs(
                    limit=AUDIT_LOG_LIMIT, action=discord.AuditLogAction.member_disconnect
                )
                if not _by_a_bot(entry)
            }
        except (discord.HTTPException, aiohttp.ClientError, OSError) as exc:  # OSError: timeouts
            self._failed(exc)
            return None

    def _failed(self, exc: Exception) -> None:
        """One WARNING per kind of failure per process; every failure at DEBUG, by type only."""
        name = type(exc).__name__
        if isinstance(exc, discord.Forbidden):  # no View Audit Log
            kind, warning = "forbidden", AUDIT_LOG_UNREADABLE
        elif isinstance(exc, discord.HTTPException):  # a server error, say: not the permission
            kind, warning = "failed", AUDIT_LOG_FAILED.format(name)
        else:  # no answer at all: a connection error or a timeout
            kind, warning = "unreachable", AUDIT_LOG_UNREACHABLE.format(name)
        if kind not in self._warned:
            self._warned.add(kind)
            log.warning(warning)
        log.debug("reading the audit log failed: %s", name)


def _count(entry: Any) -> int:
    """How many disconnects the entry aggregates; 0 when Discord sent no count."""
    return int(getattr(entry.extra, "count", 0))


def _by_a_bot(entry: Any) -> bool:
    """True when a bot made the entry. An unknown user (left the server, say) is not a bot."""
    return entry.user is not None and bool(entry.user.bot)


def read_census(guild: discord.Guild | None, own_id: int, exclude: Collection[int]) -> VoiceCensus:
    """Who is in each voice channel Amir can view, from discord.py's cache, in the server's
    channel order. A channel hidden from his role is left out, people and all: who is there is
    not his to see.

    Humans are non-bot members other than Amir. A channel is a candidate unless it is the AFK
    channel, a stage channel, excluded, capped by a user limit, or one where Amir lacks connect
    or speak. Stage channels are in the census all the same, so a drag onto one can pin him
    there. Never raises: a missing guild or any error gives the empty census, with the
    exception's type at DEBUG.
    """
    if guild is None:
        return VoiceCensus.EMPTY
    try:
        afk = guild.afk_channel
        afk_id = None if afk is None else afk.id
        voice = list(guild.voice_channels)
        stages = list(guild.stage_channels)
        stage_ids = {stage.id for stage in stages}
        views = []
        for channel in sorted([*voice, *stages], key=lambda c: c.position):
            perms = channel.permissions_for(guild.me)
            if not perms.view_channel:
                continue
            can_speak = bool(perms.speak)
            candidate = (
                channel.id not in stage_ids
                and channel.id != afk_id
                and channel.id not in exclude
                and not channel.user_limit
                and bool(perms.connect)
                and can_speak
            )
            humans = frozenset(m.id for m in channel.members if not m.bot and m.id != own_id)
            views.append(ChannelView(channel.id, humans, candidate, can_speak))
        return VoiceCensus(channels=tuple(views), afk_channel_id=afk_id)
    except Exception as exc:
        log.debug("reading the voice census failed: %s", type(exc).__name__)
        return VoiceCensus.EMPTY
