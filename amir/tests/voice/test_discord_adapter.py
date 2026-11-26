import asyncio
import inspect
import logging
import subprocess
import threading
from collections.abc import AsyncIterator, Callable
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp
import discord
import pytest
from discord.voice_state import VoiceConnectionState

from amir.voice.discord_adapter import (
    AUDIT_LOG_FAILED,
    AUDIT_LOG_LIMIT,
    AUDIT_LOG_UNREACHABLE,
    AUDIT_LOG_UNREADABLE,
    HEAR_WAIT_RAN_OUT,
    KICK_WINDOW_SECONDS,
    VOICE_CONNECT_PAUSE_SECONDS,
    AmirVoiceClient,
    DiscordKickDetector,
    DiscordPlayer,
    DiscordVoiceConnector,
    PausingConnectionState,
    build_source,
    can_hear_amir,
    read_census,
)
from amir.voice.follow import VoiceCensus
from amir.voice.session import PlaybackError, VoiceConfigError, VoiceSession

LOUDNORM = "loudnorm=I=-16:TP=-1.5:LRA=11"
WARFRAME_ID = 3001
OTHER_ID = 3002
AMIR_ID = 4000
LISTENER_ID = 4001


class FakeGuild:
    """Holds the bot's own deafen flag the way Discord does: every voice-state send sets it."""

    def __init__(self) -> None:
        self.voice_client: FakeVoiceClient | None = None
        self.self_deaf = False
        self.voice_state_calls: list[dict[str, Any]] = []

    async def change_voice_state(
        self,
        *,
        channel: "FakeVoiceChannel | None",
        self_mute: bool = False,
        self_deaf: bool = False,
    ) -> None:
        # Same defaults as discord.Guild.change_voice_state: leaving out self_deaf undeafens.
        self.voice_state_calls.append(
            {"channel": channel, "self_mute": self_mute, "self_deaf": self_deaf}
        )
        self.self_deaf = self_deaf


class FakeVoiceChannel(discord.VoiceChannel):
    """A real VoiceChannel subtype (passes isinstance) without discord.py's state machinery."""

    def __init__(self, channel_id: int, guild: FakeGuild) -> None:
        self.id = channel_id
        self.guild = guild  # type: ignore[assignment]
        self.connect_calls: list[dict[str, Any]] = []

    def __repr__(self) -> str:
        return f"<FakeVoiceChannel {self.id}>"

    async def connect(self, **kwargs: Any) -> "FakeVoiceClient":  # type: ignore[override]
        self.connect_calls.append(kwargs)
        self.guild.self_deaf = kwargs.get("self_deaf", False)
        client = FakeVoiceClient(self)
        self.guild.voice_client = client
        return client


class FakeTextChannel(discord.TextChannel):
    def __init__(self, channel_id: int) -> None:
        self.id = channel_id

    def __repr__(self) -> str:
        return f"<FakeTextChannel {self.id}>"


class FakeVoiceClient:
    """Mimics discord.VoiceClient; finish() does what the audio thread does: after(), cleanup()."""

    def __init__(
        self,
        channel: FakeVoiceChannel | None = None,
        *,
        connected: bool = True,
        playing: bool = False,
        on_play: Callable[["FakeVoiceClient"], None] | None = None,
    ) -> None:
        self.channel = channel
        self.connected = connected
        self.playing = playing
        self.on_play = on_play
        self.sources: list[discord.AudioSource] = []
        self.processes: list[subprocess.Popen] = []
        self.moves: list[tuple[FakeVoiceChannel, float]] = []  # Amir never moves himself
        self.disconnect_calls: list[bool] = []
        self.stopped = False
        self.play_error: Exception | None = None
        self._after: Callable[[Exception | None], Any] | None = None

    def is_connected(self) -> bool:
        return self.connected

    def is_playing(self) -> bool:
        return self.playing

    def play(
        self, source: discord.AudioSource, *, after: Callable[[Exception | None], Any]
    ) -> None:
        if self.play_error is not None:
            raise self.play_error
        self.sources.append(source)
        self.processes.append(source._process)  # the real FFmpeg child, to prove it is reaped
        self.playing = True
        self._after = after
        if self.on_play is not None:
            self.on_play(self)

    def finish(self, error: Exception | None) -> None:
        self.playing = False
        self.call_after(error)
        self.sources[-1].cleanup()

    def call_after(self, error: Exception | None) -> None:
        assert self._after is not None
        self._after(error)

    def stop(self) -> None:
        self.stopped = True
        if self.playing:
            self.finish(None)

    async def move_to(
        self,
        channel: FakeVoiceChannel,
        *,
        timeout: float,  # noqa: ASYNC109 - as discord.py
    ) -> None:
        self.moves.append((channel, timeout))
        # As discord.py 2.7.1's VoiceConnectionState._move_to: no deafen flag is sent.
        await channel.guild.change_voice_state(channel=channel)
        self.channel = channel

    async def disconnect(self, *, force: bool) -> None:
        self.disconnect_calls.append(force)
        self.connected = False
        if self.channel is not None and self.channel.guild.voice_client is self:
            self.channel.guild.voice_client = None


class FakeClient:
    def __init__(
        self,
        cached: dict[int, discord.abc.GuildChannel] | None = None,
        remote: dict[int, discord.abc.GuildChannel] | None = None,
    ) -> None:
        self.cached = cached or {}
        self.remote = remote or {}
        self.fetched: list[int] = []

    def get_channel(self, channel_id: int) -> discord.abc.GuildChannel | None:
        return self.cached.get(channel_id)

    async def fetch_channel(self, channel_id: int) -> discord.abc.GuildChannel:
        self.fetched.append(channel_id)
        return self.remote[channel_id]


def finish_later(error: Exception | None) -> Callable[[FakeVoiceClient], None]:
    def on_play(vc: FakeVoiceClient) -> None:
        asyncio.get_running_loop().call_later(0.01, vc.finish, error)

    return on_play


def finish_from_thread(vc: FakeVoiceClient) -> None:
    threading.Timer(0.01, vc.finish, args=(None,)).start()


async def no_sleep(delay: float) -> None:
    return None


# --- build_source (real FFmpeg, no Discord) ---------------------------------------------


def test_opus_source_from_real_clip(voice_clip: Path, ffmpeg_exe: str):
    source = build_source(voice_clip, ffmpeg_exe, LOUDNORM)
    try:
        assert isinstance(source, discord.FFmpegOpusAudio)
        assert source.is_opus()
        packet = source.read()
        assert isinstance(packet, bytes)
        assert len(packet) > 0
    finally:
        source.cleanup()


def test_source_command_line(voice_clip: Path, ffmpeg_exe: str):
    source = build_source(voice_clip, ffmpeg_exe, LOUDNORM)
    try:
        args = list(source._process.args)
    finally:
        source.cleanup()
    assert args[0] == ffmpeg_exe
    assert args[1:4] == ["-nostdin", "-i", str(voice_clip)]
    assert "-vn" in args
    assert args[args.index("-af") + 1] == LOUDNORM
    assert args[-1] == "pipe:1"


# --- DiscordPlayer ------------------------------------------------------------------------


async def test_player_waits_for_after_callback(voice_clip: Path, ffmpeg_exe: str):
    vc = FakeVoiceClient(on_play=finish_later(None))
    player = DiscordPlayer(lambda: vc, ffmpeg_exe, LOUDNORM)
    await asyncio.wait_for(player.play(voice_clip, LISTENER_ID), 2.0)
    assert len(vc.sources) == 1
    assert isinstance(vc.sources[0], discord.FFmpegOpusAudio)
    assert not vc.playing
    assert vc.processes[0].poll() is not None


async def test_player_raises_on_error(voice_clip: Path, ffmpeg_exe: str):
    vc = FakeVoiceClient(on_play=finish_later(RuntimeError("opus packet stream broke")))
    player = DiscordPlayer(lambda: vc, ffmpeg_exe, LOUDNORM)
    with pytest.raises(PlaybackError) as caught:
        await asyncio.wait_for(player.play(voice_clip, LISTENER_ID), 2.0)
    assert isinstance(caught.value.__cause__, RuntimeError)
    assert caught.value.clip_fault is True  # the audio source failed: the clip's fault


async def test_player_after_from_audio_thread(voice_clip: Path, ffmpeg_exe: str):
    # No other timer may wake the loop here: a result set from the thread without
    # call_soon_threadsafe would only be noticed when the 10 s playback timeout fires.
    vc = FakeVoiceClient(on_play=finish_from_thread)
    player = DiscordPlayer(lambda: vc, ffmpeg_exe, LOUDNORM, timeout=10.0)
    loop = asyncio.get_running_loop()
    started = loop.time()
    await player.play(voice_clip, LISTENER_ID)
    assert loop.time() - started < 5.0
    assert len(vc.sources) == 1
    assert not vc.playing


@pytest.mark.parametrize("provided", [None, FakeVoiceClient(connected=False)])
async def test_player_raises_when_not_connected(
    provided: FakeVoiceClient | None, voice_clip: Path, ffmpeg_exe: str
):
    player = DiscordPlayer(lambda: provided, ffmpeg_exe, LOUDNORM)
    with pytest.raises(PlaybackError) as caught:
        await player.play(voice_clip, LISTENER_ID)
    assert caught.value.clip_fault is False
    if provided is not None:
        assert provided.sources == []


async def test_player_never_interrupts(voice_clip: Path, ffmpeg_exe: str):
    vc = FakeVoiceClient(playing=True)
    player = DiscordPlayer(lambda: vc, ffmpeg_exe, LOUDNORM, timeout=0.5)
    with pytest.raises(PlaybackError) as caught:
        await player.play(voice_clip, LISTENER_ID)
    assert caught.value.clip_fault is False
    assert vc.sources == []
    assert not vc.stopped
    assert vc.playing


async def test_player_timeout_stops_playback(voice_clip: Path, ffmpeg_exe: str):
    vc = FakeVoiceClient()  # never finishes on its own
    player = DiscordPlayer(lambda: vc, ffmpeg_exe, LOUDNORM, timeout=0.05)
    with pytest.raises(PlaybackError) as caught:
        await asyncio.wait_for(player.play(voice_clip, LISTENER_ID), 2.0)
    assert caught.value.clip_fault is False
    assert vc.stopped
    assert not vc.playing
    assert vc.processes[0].poll() is not None


async def test_late_after_callback_is_ignored(voice_clip: Path, ffmpeg_exe: str):
    loop = asyncio.get_running_loop()
    reported: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: reported.append(context))
    try:
        vc = FakeVoiceClient()  # never finishes on its own
        player = DiscordPlayer(lambda: vc, ffmpeg_exe, LOUDNORM, timeout=0.05)
        with pytest.raises(PlaybackError):
            await player.play(voice_clip, LISTENER_ID)
        # stop() already fired after(None); the audio thread may still report late, too.
        vc.call_after(RuntimeError("late report from the audio thread"))
        for _ in range(3):
            await asyncio.sleep(0)
    finally:
        loop.set_exception_handler(None)
    assert reported == []


async def test_player_cancel_stops_playback(voice_clip: Path, ffmpeg_exe: str):
    vc = FakeVoiceClient()
    player = DiscordPlayer(lambda: vc, ffmpeg_exe, LOUDNORM)
    task = asyncio.create_task(player.play(voice_clip, LISTENER_ID))
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert vc.stopped
    assert vc.processes[0].poll() is not None


async def test_player_rejected_play_raises_and_reaps_ffmpeg(
    voice_clip: Path, ffmpeg_exe: str, monkeypatch: pytest.MonkeyPatch
):
    from amir.voice import discord_adapter

    spawned: list[subprocess.Popen] = []

    def recording_build_source(path: Path, ffmpeg: str, audio_filter: str):
        source = build_source(path, ffmpeg, audio_filter)
        spawned.append(source._process)
        return source

    monkeypatch.setattr(discord_adapter, "build_source", recording_build_source)
    vc = FakeVoiceClient()
    vc.play_error = discord.ClientException("Already playing audio.")
    player = DiscordPlayer(lambda: vc, ffmpeg_exe, LOUDNORM)
    with pytest.raises(PlaybackError) as caught:
        await player.play(voice_clip, LISTENER_ID)
    assert isinstance(caught.value.__cause__, discord.ClientException)
    assert caught.value.clip_fault is False
    assert len(spawned) == 1
    assert spawned[0].poll() is not None


async def test_player_missing_ffmpeg_is_a_playback_error(voice_clip: Path, tmp_path: Path):
    vc = FakeVoiceClient()
    player = DiscordPlayer(lambda: vc, str(tmp_path / "no-ffmpeg-here"), LOUDNORM)
    with pytest.raises(PlaybackError) as caught:
        await player.play(voice_clip, LISTENER_ID)
    assert caught.value.clip_fault is False
    assert vc.sources == []


# --- DiscordPlayer: waiting until the listener can hear Amir (DAVE) ----------------------


class FakeDaveSession:
    """The two things davey 0.1.7's DaveSession tells: ready, and the group's user IDs."""

    def __init__(self, *, ready: bool, user_ids: list[str]) -> None:
        self.ready = ready
        self.user_ids = user_ids

    def get_user_ids(self) -> list[str]:
        return list(self.user_ids)


def with_dave(vc: FakeVoiceClient, session: FakeDaveSession | None, **pending: int) -> None:
    """Gives `vc` the DAVE fields of discord.py 2.7.1's VoiceConnectionState."""
    vc._connection = SimpleNamespace(  # type: ignore[attr-defined]
        dave_protocol_version=1,
        dave_session=session,
        dave_pending_transitions={int(k[1:]): v for k, v in pending.items()},
    )


def later(delay: float, change: Callable[[], None]) -> list[float]:
    """Runs `change` after `delay`; the returned list gets the loop time it ran at."""
    loop = asyncio.get_running_loop()
    ran_at: list[float] = []

    def run() -> None:
        change()
        ran_at.append(loop.time())

    loop.call_later(delay, run)
    return ran_at


def timed_play(vc: FakeVoiceClient) -> list[float]:
    """Finishes each clip as the audio thread would; the returned list gets each start time."""
    played_at: list[float] = []

    def on_play(v: FakeVoiceClient) -> None:
        played_at.append(asyncio.get_running_loop().time())
        finish_later(None)(v)

    vc.on_play = on_play
    return played_at


async def test_player_waits_until_the_listener_is_in_the_encrypted_call(
    voice_clip: Path, ffmpeg_exe: str, caplog: pytest.LogCaptureFixture
):
    # The arrival has just joined: Amir's session is ready, the listener is not yet in it.
    session = FakeDaveSession(ready=True, user_ids=[str(AMIR_ID)])
    vc = FakeVoiceClient()
    played_at = timed_play(vc)
    with_dave(vc, session)
    player = DiscordPlayer(lambda: vc, ffmpeg_exe, LOUDNORM)
    joined_at = later(0.15, lambda: session.user_ids.append(str(LISTENER_ID)))
    with caplog.at_level(logging.INFO, logger="amir.voice.discord_adapter"):
        await asyncio.wait_for(player.play(voice_clip, LISTENER_ID), 2.0)
    assert len(played_at) == 1 and len(joined_at) == 1
    assert played_at[0] >= joined_at[0]
    assert any("waited" in r.getMessage() for r in caplog.records)


async def test_player_waits_for_amirs_own_session_to_be_ready(voice_clip: Path, ffmpeg_exe: str):
    # Amir has just connected: his session joins the group a moment later.
    session = FakeDaveSession(ready=False, user_ids=[])
    vc = FakeVoiceClient()
    played_at = timed_play(vc)
    with_dave(vc, session)
    player = DiscordPlayer(lambda: vc, ffmpeg_exe, LOUDNORM)

    def welcome() -> None:
        session.ready = True
        session.user_ids += [str(AMIR_ID), str(LISTENER_ID)]

    ready_at = later(0.15, welcome)
    await asyncio.wait_for(player.play(voice_clip, LISTENER_ID), 2.0)
    assert len(played_at) == 1 and len(ready_at) == 1
    assert played_at[0] >= ready_at[0]


async def test_player_waits_out_a_pending_key_change(voice_clip: Path, ffmpeg_exe: str):
    session = FakeDaveSession(ready=True, user_ids=[str(AMIR_ID), str(LISTENER_ID)])
    vc = FakeVoiceClient()
    played_at = timed_play(vc)
    with_dave(vc, session, t7=1)
    player = DiscordPlayer(lambda: vc, ffmpeg_exe, LOUDNORM)
    pending = vc._connection.dave_pending_transitions  # type: ignore[attr-defined]
    executed_at = later(0.15, pending.clear)
    await asyncio.wait_for(player.play(voice_clip, LISTENER_ID), 2.0)
    assert len(played_at) == 1 and len(executed_at) == 1
    assert played_at[0] >= executed_at[0]


async def test_player_plays_at_once_without_end_to_end_encryption(
    voice_clip: Path, ffmpeg_exe: str
):
    vc = FakeVoiceClient(on_play=finish_later(None))
    with_dave(vc, None)
    vc._connection.dave_protocol_version = 0  # type: ignore[attr-defined]
    player = DiscordPlayer(lambda: vc, ffmpeg_exe, LOUDNORM, hear_wait=0.0)
    await asyncio.wait_for(player.play(voice_clip, LISTENER_ID), 2.0)
    assert len(vc.sources) == 1


async def test_player_plays_anyway_once_the_wait_runs_out(
    voice_clip: Path, ffmpeg_exe: str, caplog: pytest.LogCaptureFixture
):
    session = FakeDaveSession(ready=True, user_ids=[str(AMIR_ID)])  # the listener never shows
    vc = FakeVoiceClient(on_play=finish_later(None))
    with_dave(vc, session)
    player = DiscordPlayer(lambda: vc, ffmpeg_exe, LOUDNORM, hear_wait=0.1)
    with caplog.at_level(logging.WARNING, logger="amir.voice.discord_adapter"):
        await asyncio.wait_for(player.play(voice_clip, LISTENER_ID), 2.0)
    assert len(vc.sources) == 1
    assert [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING] == [
        HEAR_WAIT_RAN_OUT
    ]


def test_can_hear_amir_reads_the_installed_discord_pys_connection_state():
    # If discord.py renames these, the wait would silently stop working: fail here instead.
    assert "self._connection: VoiceConnectionState" in inspect.getsource(discord.VoiceClient)
    state = VoiceConnectionState(SimpleNamespace())  # type: ignore[arg-type]
    vc = SimpleNamespace(_connection=state)
    assert can_hear_amir(vc, LISTENER_ID)  # type: ignore[arg-type] # no DAVE negotiated yet
    state.dave_protocol_version = 1  # DAVE negotiated, session not created yet
    assert not can_hear_amir(vc, LISTENER_ID)  # type: ignore[arg-type]
    state.dave_session = FakeDaveSession(ready=True, user_ids=[str(LISTENER_ID)])  # type: ignore
    assert can_hear_amir(vc, LISTENER_ID)  # type: ignore[arg-type]
    state.dave_pending_transitions[3] = 1
    assert not can_hear_amir(vc, LISTENER_ID)  # type: ignore[arg-type]


async def test_player_left_voice_during_the_wait_raises(voice_clip: Path, ffmpeg_exe: str):
    session = FakeDaveSession(ready=True, user_ids=[str(AMIR_ID)])
    vc = FakeVoiceClient()
    with_dave(vc, session)
    player = DiscordPlayer(lambda: vc, ffmpeg_exe, LOUDNORM)
    later(0.1, lambda: setattr(vc, "connected", False))
    with pytest.raises(PlaybackError) as caught:
        await asyncio.wait_for(player.play(voice_clip, LISTENER_ID), 2.0)
    assert caught.value.clip_fault is False
    assert vc.sources == []


async def test_a_test_greeting_waits_only_for_a_key_change(
    voice_clip: Path, ffmpeg_exe: str, caplog: pytest.LogCaptureFixture
):
    # /amir voice test greets nobody in particular: no member to wait for in the group.
    session = FakeDaveSession(ready=True, user_ids=[str(AMIR_ID)])
    vc = FakeVoiceClient()
    played_at = timed_play(vc)
    with_dave(vc, session)
    player = DiscordPlayer(lambda: vc, ffmpeg_exe, LOUDNORM, hear_wait=0.5)
    with caplog.at_level(logging.INFO, logger="amir.voice.discord_adapter"):
        await asyncio.wait_for(player.play(voice_clip, None), 2.0)
    assert len(played_at) == 1
    assert caplog.records == []  # at once: no wait to log, and no HEAR_WAIT_RAN_OUT

    # A key change is pending: he plays once it clears, still with no warning.
    caplog.clear()
    with_dave(vc, session, t7=1)
    pending = vc._connection.dave_pending_transitions  # type: ignore[attr-defined]
    executed_at = later(0.15, pending.clear)
    with caplog.at_level(logging.INFO, logger="amir.voice.discord_adapter"):
        await asyncio.wait_for(player.play(voice_clip, None), 2.0)
    assert len(played_at) == 2 and len(executed_at) == 1
    assert played_at[1] >= executed_at[0]
    [message] = [r.getMessage() for r in caplog.records]  # no HEAR_WAIT_RAN_OUT
    assert message.startswith("greeting waited ")


def test_with_no_listener_only_amirs_session_and_key_changes_count():
    state = VoiceConnectionState(SimpleNamespace())  # type: ignore[arg-type]
    vc = SimpleNamespace(_connection=state)
    assert can_hear_amir(vc, None)  # type: ignore[arg-type] # no DAVE negotiated yet
    state.dave_protocol_version = 1  # DAVE negotiated, session not created yet
    assert not can_hear_amir(vc, None)  # type: ignore[arg-type]
    dave = FakeDaveSession(ready=False, user_ids=[])
    state.dave_session = dave  # type: ignore[assignment]
    assert not can_hear_amir(vc, None)  # type: ignore[arg-type]
    dave.ready = True  # nobody else in the group, and none needed
    assert can_hear_amir(vc, None)  # type: ignore[arg-type]
    assert not can_hear_amir(vc, LISTENER_ID)  # type: ignore[arg-type] # a member still is
    state.dave_pending_transitions[3] = 1
    assert not can_hear_amir(vc, None)  # type: ignore[arg-type]


# --- DiscordVoiceConnector ---------------------------------------------------------------


def warframe_setup(*, cached: bool = True) -> tuple[FakeClient, FakeVoiceChannel, FakeGuild]:
    guild = FakeGuild()
    channel = FakeVoiceChannel(WARFRAME_ID, guild)
    client = FakeClient(cached={WARFRAME_ID: channel} if cached else None)
    if not cached:
        client.remote[WARFRAME_ID] = channel
    return client, channel, guild


def two_channel_setup() -> tuple[FakeClient, FakeGuild, dict[int, FakeVoiceChannel]]:
    """Warframe and another voice channel of the same guild, both cached."""
    guild = FakeGuild()
    channels = {cid: FakeVoiceChannel(cid, guild) for cid in (WARFRAME_ID, OTHER_ID)}
    return FakeClient(cached=dict(channels)), guild, channels


@pytest.mark.parametrize("channel_id", [WARFRAME_ID, OTHER_ID])
async def test_connector_joins_the_requested_channel_self_deafened(channel_id: int):
    client, guild, channels = two_channel_setup()
    connector = DiscordVoiceConnector(client)
    assert not connector.is_connected()
    assert connector.voice_client() is None
    await connector.connect(channel_id, 15.0)
    assert channels[channel_id].connect_calls == [
        {"timeout": 15.0, "reconnect": True, "self_deaf": True, "cls": AmirVoiceClient}
    ]
    assert [c.connect_calls for c in channels.values() if c.id != channel_id] == [[]]
    assert guild.self_deaf
    assert connector.is_connected()
    assert connector.current_channel_id() == channel_id
    assert connector.voice_client() is guild.voice_client
    assert client.fetched == []


async def test_connector_current_channel_id():
    client, guild, channels = two_channel_setup()
    connector = DiscordVoiceConnector(client)
    assert connector.current_channel_id() is None  # before any connect
    await connector.connect(WARFRAME_ID, 15.0)
    assert connector.current_channel_id() == WARFRAME_ID
    vc = guild.voice_client
    assert vc is not None
    vc.channel = channels[OTHER_ID]  # dragged: still connected, in another channel
    assert connector.current_channel_id() == OTHER_ID
    assert connector.is_connected()
    assert connector.voice_client() is vc  # the player plays wherever Amir is
    vc.connected = False  # registered but no longer connected (e.g. soft-disconnected)
    assert connector.current_channel_id() is None
    assert not connector.is_connected()
    assert connector.voice_client() is None
    vc.connected = True
    await connector.disconnect()
    assert connector.current_channel_id() is None


async def test_connector_never_moves_itself():
    # Connecting to another channel is a leave, then a fresh self-deafened join: never move_to,
    # which would undeafen him, and never a second connection.
    client, guild, channels = two_channel_setup()
    connector = DiscordVoiceConnector(client)
    await connector.connect(WARFRAME_ID, 15.0)
    first = guild.voice_client
    assert first is not None
    await connector.connect(OTHER_ID, 15.0)
    assert first.moves == []
    assert first.disconnect_calls == [True]
    assert channels[OTHER_ID].connect_calls == [
        {"timeout": 15.0, "reconnect": True, "self_deaf": True, "cls": AmirVoiceClient}
    ]
    assert guild.voice_state_calls == []  # nothing ever sent without the deafen flag
    assert guild.self_deaf
    assert guild.voice_client is not first
    assert connector.current_channel_id() == OTHER_ID


async def test_connector_fetches_uncached_channel():
    client, channel, _ = warframe_setup(cached=False)
    connector = DiscordVoiceConnector(client)
    await connector.connect(WARFRAME_ID, 15.0)
    assert client.fetched == [WARFRAME_ID]
    assert len(channel.connect_calls) == 1
    assert connector.is_connected()


async def test_connector_rejects_non_voice_channel():
    client = FakeClient(cached={WARFRAME_ID: FakeTextChannel(WARFRAME_ID)})
    connector = DiscordVoiceConnector(client)
    with pytest.raises(VoiceConfigError, match="not a voice channel"):
        await connector.connect(WARFRAME_ID, 15.0)
    assert not connector.is_connected()


async def test_connector_in_another_channel_leaves_it_and_connects_afresh():
    # E.g. dragged elsewhere before this connector ever learned the guild: never a move back,
    # which would undeafen him, and never a second connection.
    client, channel, guild = warframe_setup()
    dragged = FakeVoiceClient(FakeVoiceChannel(OTHER_ID, guild))
    guild.voice_client = dragged
    guild.self_deaf = True  # a drag keeps Amir's own deafen flag
    connector = DiscordVoiceConnector(client)
    await connector.connect(WARFRAME_ID, 15.0)  # first resolve: learns the guild
    assert dragged.moves == []
    assert dragged.disconnect_calls == [True]
    assert channel.connect_calls == [
        {"timeout": 15.0, "reconnect": True, "self_deaf": True, "cls": AmirVoiceClient}
    ]
    assert guild.voice_state_calls == []  # nothing ever sent without the deafen flag
    assert guild.self_deaf
    assert connector.is_connected()
    assert guild.voice_client is not dragged


async def test_connector_already_in_target_does_nothing():
    client, channel, guild = warframe_setup()
    present = FakeVoiceClient(channel)
    guild.voice_client = present
    connector = DiscordVoiceConnector(client)
    await connector.connect(WARFRAME_ID, 15.0)
    assert guild.voice_state_calls == []
    assert present.moves == []
    assert present.disconnect_calls == []
    assert channel.connect_calls == []
    assert connector.is_connected()


async def test_stale_registration_is_cleared_before_connecting():
    client, channel, guild = warframe_setup()
    stale = FakeVoiceClient(channel, connected=False)  # left by a cancelled connect
    guild.voice_client = stale
    connector = DiscordVoiceConnector(client)
    await connector.connect(WARFRAME_ID, 15.0)
    assert stale.disconnect_calls == [True]
    assert len(channel.connect_calls) == 1
    assert guild.voice_client is not stale
    assert connector.is_connected()
    assert guild.self_deaf


async def test_misconfigured_channel_fails_fast_and_is_logged(caplog: pytest.LogCaptureFixture):
    client = FakeClient(cached={WARFRAME_ID: FakeTextChannel(WARFRAME_ID)})
    delays: list[float] = []

    async def recording_sleep(delay: float) -> None:
        delays.append(delay)

    session = VoiceSession(
        DiscordVoiceConnector(client),
        grace_seconds=60.0,
        connect_timeout=15.0,
        max_attempts=5,
        sleep=recording_sleep,
    )
    with caplog.at_level(logging.WARNING, logger="amir.voice.session"):
        assert await session.ensure_connected(WARFRAME_ID) is False
    assert session.state == "failed"
    assert delays == []
    assert "is a FakeTextChannel, not a voice channel" in caplog.text


async def test_connector_disconnect():
    client, channel, guild = warframe_setup()
    connector = DiscordVoiceConnector(client)
    assert await connector.disconnect() is False  # nothing to do before any connect
    await connector.connect(WARFRAME_ID, 15.0)
    vc = guild.voice_client
    assert vc is not None
    assert await connector.disconnect() is True
    assert vc.disconnect_calls == [True]
    assert not connector.is_connected()
    assert guild.voice_client is None
    assert await connector.disconnect() is False  # idle again: still safe
    assert vc.disconnect_calls == [True]


async def test_connector_disconnect_from_another_channel():
    client, channel, guild = warframe_setup()
    connector = DiscordVoiceConnector(client)
    await connector.connect(WARFRAME_ID, 15.0)
    vc = guild.voice_client
    assert vc is not None
    vc.channel = FakeVoiceChannel(OTHER_ID, guild)  # dragged out of Warframe
    assert connector.is_connected()  # connected means in voice, in any channel
    assert await connector.disconnect() is True
    assert vc.disconnect_calls == [True]
    assert guild.voice_client is None


async def test_connector_disconnect_of_a_stale_client_reports_not_in_voice():
    client, channel, guild = warframe_setup()
    connector = DiscordVoiceConnector(client)
    await connector.connect(WARFRAME_ID, 15.0)
    vc = guild.voice_client
    assert vc is not None
    vc.connected = False  # registered but no longer connected (e.g. soft-disconnected)
    assert await connector.disconnect() is False  # he was not in voice ...
    assert vc.disconnect_calls == [True]  # ... but the stale client is still cleared
    assert guild.voice_client is None


async def test_session_leaves_from_another_channel():
    # Dragged elsewhere while Warframe is empty: the grace-period leave must still leave.
    client, channel, guild = warframe_setup()
    connector = DiscordVoiceConnector(client)
    session = VoiceSession(
        connector, grace_seconds=60.0, connect_timeout=15.0, max_attempts=5, sleep=no_sleep
    )
    assert await session.ensure_connected(WARFRAME_ID) is True
    vc = guild.voice_client
    assert vc is not None
    vc.channel = FakeVoiceChannel(OTHER_ID, guild)
    await session.disconnect()
    assert vc.disconnect_calls == [True]
    assert guild.voice_client is None
    assert session.state == "idle"


# --- DiscordKickDetector -------------------------------------------------------------------

GUILD_ID = 2001


class FakeAuditGuild:
    """guild.audit_logs as discord.py has it: an async iterator, newest first, that fails on
    its first step (the HTTP request) when the audit log can't be read."""

    def __init__(self) -> None:
        self.entries: list[SimpleNamespace] = []
        self.error: Exception | None = None  # raised by every read while set
        self.queries: list[tuple[int | None, discord.AuditLogAction]] = []

    async def audit_logs(
        self, *, limit: int | None, action: discord.AuditLogAction
    ) -> AsyncIterator[SimpleNamespace]:
        self.queries.append((limit, action))
        if self.error is not None:
            raise self.error
        for entry in list(self.entries):
            yield entry


class AuditClient:
    def __init__(self, guild: FakeAuditGuild | None) -> None:
        self.guild = guild
        self.asked: list[int] = []

    def get_guild(self, guild_id: int) -> FakeAuditGuild | None:
        self.asked.append(guild_id)
        return self.guild


def disconnects(
    entry_id: int, clock, *, count: int = 1, age: float = 0.0, bot: bool = False
) -> SimpleNamespace:
    """A member_disconnect entry as discord.py gives it: no target; extra.count; the
    moderator as `user`; created_at from its snowflake, `age` seconds before now."""
    return SimpleNamespace(
        id=entry_id,
        action=discord.AuditLogAction.member_disconnect,
        target=None,
        user=SimpleNamespace(bot=bot),
        extra=SimpleNamespace(count=count),
        created_at=clock.now() - timedelta(seconds=age),
    )


def http_error(status: int, reason: str) -> SimpleNamespace:
    return SimpleNamespace(status=status, reason=reason)


def kick_setup(clock) -> tuple[FakeAuditGuild, AuditClient, DiscordKickDetector]:
    guild = FakeAuditGuild()
    client = AuditClient(guild)
    return guild, client, DiscordKickDetector(client, GUILD_ID, clock)


async def test_a_new_entry_within_a_minute_is_a_kick(clock):
    guild, client, detector = kick_setup(clock)
    guild.entries = [disconnects(11, clock, count=3, age=3600)]  # an hour old: someone else
    await detector.snapshot()
    clock.advance(600)  # Amir has been in voice ten minutes
    guild.entries.insert(0, disconnects(12, clock, age=2))

    assert await detector.was_kicked() is True
    assert client.asked == [GUILD_ID, GUILD_ID]
    member_disconnect = discord.AuditLogAction.member_disconnect
    assert guild.queries == [(AUDIT_LOG_LIMIT, member_disconnect)] * 2
    assert AUDIT_LOG_LIMIT == 10


@pytest.mark.parametrize(("age", "kicked"), [(59.0, True), (60.0, True), (61.0, False)])
async def test_only_a_recent_new_entry_counts(clock, age: float, kicked: bool):
    guild, _, detector = kick_setup(clock)
    await detector.snapshot()
    guild.entries = [disconnects(12, clock, age=age)]

    assert await detector.was_kicked() is kicked
    assert KICK_WINDOW_SECONDS == 60


async def test_a_raised_count_is_a_kick(clock):
    # Discord aggregates a moderator's disconnects: the count rises, created_at stays put.
    guild, _, detector = kick_setup(clock)
    guild.entries = [disconnects(11, clock, count=1, age=300)]
    await detector.snapshot()
    clock.advance(1200)
    guild.entries = [disconnects(11, clock, count=2, age=1500)]

    assert await detector.was_kicked() is True


async def test_unchanged_entries_are_not_a_kick(clock):
    guild, _, detector = kick_setup(clock)
    guild.entries = [disconnects(11, clock, count=2, age=30), disconnects(10, clock, age=900)]
    await detector.snapshot()

    assert await detector.was_kicked() is False  # e.g. a network drop


async def test_no_entries_are_not_a_kick(clock):
    _, _, detector = kick_setup(clock)
    await detector.snapshot()

    assert await detector.was_kicked() is False


async def test_a_bots_disconnects_are_not_a_kick(clock):
    guild, _, detector = kick_setup(clock)
    guild.entries = [disconnects(11, clock, count=1, age=300, bot=True)]
    await detector.snapshot()
    guild.entries = [
        disconnects(12, clock, age=1, bot=True),
        disconnects(11, clock, count=2, age=300, bot=True),
    ]

    assert await detector.was_kicked() is False


async def test_an_entry_by_an_unknown_user_counts(clock):
    guild, _, detector = kick_setup(clock)
    await detector.snapshot()
    entry = disconnects(12, clock, age=1)
    entry.user = None  # the moderator is not in discord.py's cache or the response
    guild.entries = [entry]

    assert await detector.was_kicked() is True


async def test_an_entry_without_a_count_still_counts_when_new(clock):
    guild, _, detector = kick_setup(clock)
    await detector.snapshot()
    entry = disconnects(12, clock, age=1)
    entry.extra = None  # discord.py leaves extra unset when Discord sends no options
    guild.entries = [entry]

    assert await detector.was_kicked() is True


async def test_without_a_snapshot_only_recent_entries_count(clock):
    guild, _, detector = kick_setup(clock)
    guild.entries = [disconnects(10, clock, count=4, age=3600)]
    assert await detector.was_kicked() is False

    guild.entries.insert(0, disconnects(11, clock, age=1))
    assert await detector.was_kicked() is True


async def test_a_failed_snapshot_keeps_the_last_one(clock):
    guild, _, detector = kick_setup(clock)
    guild.entries = [disconnects(11, clock, count=1, age=300)]
    await detector.snapshot()
    guild.error = discord.HTTPException(http_error(503, "Service Unavailable"), "try later")
    await detector.snapshot()
    guild.error = None
    guild.entries = [disconnects(11, clock, count=2, age=300)]

    assert await detector.was_kicked() is True


# The real effect of an unreadable audit log, behind a prefix no other line shares.
UNTIL_READABLE = (
    "until it can be read again, a disconnect counts as accidental, and a stale snapshot can"
    " later mistake an accident for a kick"
)
SERVER_ERROR_WARNING = "audit log unavailable (DiscordServerError): " + UNTIL_READABLE


def forbidden() -> discord.Forbidden:
    return discord.Forbidden(http_error(403, "Forbidden"), "Missing Permissions")


def server_error() -> discord.DiscordServerError:
    return discord.DiscordServerError(http_error(503, "Service Unavailable"), "try later")


@pytest.mark.parametrize(
    ("error", "warning"),
    [(forbidden(), AUDIT_LOG_UNREADABLE), (server_error(), SERVER_ERROR_WARNING)],
    ids=["no-view-audit-log", "server-error"],
)
async def test_an_unreadable_audit_log_is_not_a_kick_and_warns_once(
    clock, error: Exception, warning: str, caplog: pytest.LogCaptureFixture
):
    guild, _, detector = kick_setup(clock)
    guild.error = error
    with caplog.at_level(logging.DEBUG, logger="amir.voice.discord_adapter"):
        await detector.snapshot()
        assert await detector.was_kicked() is False
        assert await detector.was_kicked() is False
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == [warning]
    assert AUDIT_LOG_UNREADABLE == (
        "can't read the audit log; grant View Audit Log so a moderator's kick keeps Amir out"
    )
    debug = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert debug == [f"reading the audit log failed: {type(error).__name__}"] * 3


async def test_a_server_error_does_not_use_up_the_permission_warning(clock, caplog):
    guild, _, detector = kick_setup(clock)
    with caplog.at_level(logging.WARNING, logger="amir.voice.discord_adapter"):
        guild.error = server_error()  # a transient outage first
        assert await detector.was_kicked() is False
        assert await detector.was_kicked() is False
        guild.error = forbidden()  # then the permission is found missing
        assert await detector.was_kicked() is False
        assert await detector.was_kicked() is False
    assert caplog.messages == [SERVER_ERROR_WARNING, AUDIT_LOG_UNREADABLE]


def test_the_once_a_process_warnings_have_their_own_prefix():
    assert AUDIT_LOG_FAILED.format("DiscordServerError") == SERVER_ERROR_WARNING
    assert AUDIT_LOG_UNREACHABLE.format("TimeoutError") == (
        "audit log unreachable (TimeoutError): " + UNTIL_READABLE
    )
    for warning in (AUDIT_LOG_FAILED, AUDIT_LOG_UNREACHABLE, AUDIT_LOG_UNREADABLE):
        assert not warning.startswith("reading the audit log failed")  # the per-failure lines


@pytest.mark.parametrize(
    "error",
    [
        aiohttp.ClientConnectionError("Cannot connect to host discord.com:443"),
        TimeoutError(),
        OSError(101, "Network is unreachable"),
    ],
    ids=["aiohttp", "timeout", "oserror"],
)
async def test_an_unreachable_audit_log_is_not_a_kick_and_warns_once(
    clock, error: Exception, caplog: pytest.LogCaptureFixture
):
    guild, _, detector = kick_setup(clock)
    guild.entries = [disconnects(11, clock, count=1, age=300)]
    await detector.snapshot()
    guild.error = error
    with caplog.at_level(logging.DEBUG, logger="amir.voice.discord_adapter"):
        await detector.snapshot()  # neither method raises: the contract
        assert await detector.was_kicked() is False
        assert await detector.was_kicked() is False
    guild.error = None
    guild.entries = [disconnects(11, clock, count=2, age=300)]
    assert await detector.was_kicked() is True  # the snapshot from before is kept
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings == [AUDIT_LOG_UNREACHABLE.format(type(error).__name__)]
    debug = [r.getMessage() for r in caplog.records if r.levelno == logging.DEBUG]
    assert debug == [f"reading the audit log failed: {type(error).__name__}"] * 3


async def test_cancellation_is_not_swallowed(clock, caplog):
    # Shutdown cancels a kick check or a snapshot mid-read: that must end it, not read as a
    # failed read (an accident) or keep it going.
    guild, _, detector = kick_setup(clock)
    guild.error = asyncio.CancelledError()
    with caplog.at_level(logging.DEBUG, logger="amir.voice.discord_adapter"):
        with pytest.raises(asyncio.CancelledError):
            await detector.was_kicked()
        with pytest.raises(asyncio.CancelledError):
            await detector.snapshot()
    assert not caplog.records


async def test_each_kind_of_failure_warns_once(clock, caplog):
    guild, _, detector = kick_setup(clock)
    with caplog.at_level(logging.WARNING, logger="amir.voice.discord_adapter"):
        for error in (server_error(), TimeoutError(), forbidden()) * 2:
            guild.error = error
            assert await detector.was_kicked() is False
    assert caplog.messages == [
        SERVER_ERROR_WARNING,
        AUDIT_LOG_UNREACHABLE.format("TimeoutError"),
        AUDIT_LOG_UNREADABLE,
    ]


async def test_a_snapshot_is_kept_only_if_keep_still_says_so(clock):
    guild, _, detector = kick_setup(clock)
    guild.entries = [disconnects(11, clock, count=1, age=300)]
    await detector.snapshot(keep=lambda: True)
    guild.entries = [disconnects(11, clock, count=2, age=300)]  # a kick lands meanwhile
    asked: list[int] = []

    def keep() -> bool:
        asked.append(len(guild.queries))  # asked once the read is done
        return False  # a kick check started during the read

    await detector.snapshot(keep=keep)

    assert asked == [2]
    assert await detector.was_kicked() is True  # compared with the snapshot before


async def test_before_the_guild_is_cached_nothing_is_read(clock, caplog):
    client = AuditClient(None)
    detector = DiscordKickDetector(client, GUILD_ID, clock)
    with caplog.at_level(logging.DEBUG, logger="amir.voice.discord_adapter"):
        await detector.snapshot()
        assert await detector.was_kicked() is False
    assert client.asked == [GUILD_ID, GUILD_ID]
    assert [r.levelno for r in caplog.records] == [logging.DEBUG, logging.DEBUG]


async def test_the_audit_log_is_never_logged(clock, caplog):
    guild, _, detector = kick_setup(clock)
    with caplog.at_level(logging.DEBUG, logger="amir.voice.discord_adapter"):
        await detector.snapshot()
        guild.entries = [disconnects(987654321, clock, count=7, age=1)]
        assert await detector.was_kicked() is True
    assert not caplog.records  # who disconnected whom, or how often, is never logged


# --- read_census ------------------------------------------------------------------------------

AMIR_ID = 9001


def census_member(member_id: int, *, bot: bool = False) -> Any:
    return SimpleNamespace(id=member_id, bot=bot)


def census_channel(
    channel_id: int,
    position: int,
    members: list[Any],
    *,
    user_limit: int = 0,
    view: bool = True,
    connect: bool = True,
    speak: bool = True,
) -> Any:
    perms = SimpleNamespace(view_channel=view, connect=connect, speak=speak)
    return SimpleNamespace(
        id=channel_id,
        position=position,
        members=members,
        user_limit=user_limit,
        permissions_for=lambda who: perms,
    )


def census_guild(
    channels: list[Any], afk_id: int | None = None, stages: list[Any] | None = None
) -> Any:
    afk = SimpleNamespace(id=afk_id) if afk_id is not None else None
    return SimpleNamespace(
        me=object(), voice_channels=channels, stage_channels=stages or [], afk_channel=afk
    )


def test_read_census_counts_humans_in_server_order():
    guild = census_guild(
        [
            census_channel(
                22, 1, [census_member(5), census_member(AMIR_ID), census_member(6, bot=True)]
            ),
            census_channel(21, 0, [census_member(7)]),
        ]
    )
    got = read_census(guild, AMIR_ID, [])
    assert [view.channel_id for view in got.channels] == [21, 22]
    assert got.humans_in(21) == frozenset({7})
    assert got.humans_in(22) == frozenset({5})  # no bot, not Amir
    assert got.afk_channel_id is None


def test_read_census_marks_who_amir_may_choose():
    people = [census_member(5)]
    guild = census_guild(
        [
            census_channel(31, 0, people),
            census_channel(32, 1, people),  # the AFK channel
            census_channel(33, 2, people, user_limit=3),
            census_channel(34, 3, people),  # excluded
            census_channel(35, 4, people, connect=False),
            census_channel(36, 5, people, speak=False),
        ],
        afk_id=32,
    )
    got = read_census(guild, AMIR_ID, {34})
    chosen = {view.channel_id: (view.candidate, view.can_speak) for view in got.channels}
    assert chosen == {
        31: (True, True),
        32: (False, True),
        33: (False, True),
        34: (False, True),
        35: (False, True),
        36: (False, False),
    }
    assert got.afk_channel_id == 32
    assert got.candidates_with_people() == 1


def test_read_census_leaves_out_channels_hidden_from_amir():
    # Members' privacy: a channel his role can't view (the server's "private voice") is no
    # channel to him, even if discord.py's cache knows who is in it.
    guild = census_guild(
        [
            census_channel(51, 0, [census_member(5)]),
            census_channel(52, 1, [census_member(6), census_member(7)], view=False),
        ],
        stages=[census_channel(53, 2, [census_member(8)], view=False)],
    )
    got = read_census(guild, AMIR_ID, [])
    assert [view.channel_id for view in got.channels] == [51]
    assert got.humans_in(52) == frozenset()
    assert got.humans_in(53) == frozenset()
    assert got.candidates_with_people() == 1


def test_read_census_includes_stage_channels_he_never_chooses():
    # A drag onto a stage pins him there, so the census must know who is on it; but he never
    # picks a stage himself: speaking there needs a speaker invite.
    guild = census_guild(
        [census_channel(41, 0, [census_member(5)]), census_channel(43, 2, [census_member(6)])],
        stages=[
            census_channel(42, 1, [census_member(7), census_member(8)]),
            census_channel(44, 3, [census_member(9)], speak=False),
        ],
    )
    got = read_census(guild, AMIR_ID, [])
    assert [view.channel_id for view in got.channels] == [41, 42, 43, 44]  # server order
    chosen = {view.channel_id: (view.candidate, view.can_speak) for view in got.channels}
    assert chosen == {41: (True, True), 42: (False, True), 43: (True, True), 44: (False, False)}
    assert got.humans_in(42) == frozenset({7, 8})
    assert got.candidates_with_people() == 2  # the stage's people are not his to follow


def test_census_of_a_missing_guild_is_empty():
    assert read_census(None, AMIR_ID, []) == VoiceCensus.EMPTY


def test_census_of_a_broken_guild_is_empty_and_logs_the_type_only(caplog):
    class Broken:
        afk_channel = None

        @property
        def voice_channels(self):
            raise RuntimeError("secret 12345")

    with caplog.at_level(logging.DEBUG, logger="amir.voice.discord_adapter"):
        assert read_census(Broken(), AMIR_ID, []) == VoiceCensus.EMPTY
    assert [r.getMessage() for r in caplog.records] == [
        "reading the voice census failed: RuntimeError"
    ]


# --- the pause before a new voice session identifies ----------------------------------------
# Discord's voice server sometimes closes a brand-new session with 4006 ("session no longer
# valid") when Amir identifies about 60 ms after it handed him the session (the server is close
# to Discord's voice servers); discord.py's retry a second later always succeeded.


def test_the_pause_hooks_into_the_installed_discordpy():
    # If discord.py renames these, the pause would silently stop: fail here instead.
    params = list(inspect.signature(VoiceConnectionState._connect_websocket).parameters)
    assert params == ["self", "resume"]
    assert "create_connection_state" in vars(discord.VoiceClient)
    state = AmirVoiceClient.create_connection_state(SimpleNamespace())  # type: ignore[arg-type]
    assert isinstance(state, PausingConnectionState)


async def test_a_new_voice_session_waits_before_identifying(monkeypatch: pytest.MonkeyPatch):
    steps: list[tuple[str, object]] = []

    async def connect_websocket(self: VoiceConnectionState, resume: bool) -> str:
        steps.append(("identify", resume))
        return "ws"

    async def sleep(delay: float) -> None:
        steps.append(("pause", delay))

    monkeypatch.setattr(VoiceConnectionState, "_connect_websocket", connect_websocket)
    state = PausingConnectionState(SimpleNamespace())  # type: ignore[arg-type]
    state.sleep = sleep  # type: ignore[method-assign]

    assert await state._connect_websocket(False) == "ws"
    assert steps == [("pause", VOICE_CONNECT_PAUSE_SECONDS), ("identify", False)]
    assert VOICE_CONNECT_PAUSE_SECONDS == 0.5


async def test_a_resume_does_not_wait(monkeypatch: pytest.MonkeyPatch):
    steps: list[str] = []

    async def connect_websocket(self: VoiceConnectionState, resume: bool) -> str:
        steps.append("identify")
        return "ws"

    async def sleep(delay: float) -> None:
        steps.append("pause")

    monkeypatch.setattr(VoiceConnectionState, "_connect_websocket", connect_websocket)
    state = PausingConnectionState(SimpleNamespace())  # type: ignore[arg-type]
    state.sleep = sleep  # type: ignore[method-assign]

    await state._connect_websocket(True)
    assert steps == ["identify"]
