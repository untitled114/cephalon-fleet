import asyncio
import inspect
import logging
import shutil
import threading
from collections import Counter, deque
from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import discord
import pytest

import amir.bot.client as client_module
import amir.voice.companion as companion_module
from amir import health, persona
from amir.bot.client import AmirBot, Services, build_intents, build_services
from amir.bot.tasks import TaskSupervisor
from amir.config import load_settings
from amir.config.settings import PublishingSettings, Settings
from amir.publishing.discord_sender import DiscordSender
from amir.publishing.gate import ChannelKey, GatedWriter
from amir.storage.db import Database
from amir.storage.repos import VoiceHold, VoiceHoldRepo
from amir.voice.classify import VoiceEventKind
from amir.voice.companion import VoiceEvent
from amir.voice.follow import VoiceCensus

from .harness import FakeKickDetector, census_of

EXAMPLE = Path(__file__).resolve().parents[2] / "config" / "amir.example.toml"
TOKEN = "abc.def.ghi"
OWN_ID = 1001
GUILD_ID = 2001
VOICE_ID = 3001
NEWS_ID = 3002
CYCLES_ID = 3003
VOICE_LOG_ID = 3004
ELSEWHERE_ID = 3999  # another voice channel in the guild (Arcade, say)
PRIVATE_ID = 3998  # a voice channel hidden from Amir's role ("private voice")
SECRET_ID = 3997  # another one hidden from him
MEMBER_ID = 4001
WORKERS = (
    "outbox",
    "news",
    "worldstate",
    "cycles-ticker",
    "cycles-ensure",
    "announcer",
    "greetings",
    "heartbeat",
)


def make_settings(tmp_path: Path, ffmpeg: str, clip: Path, *, publishing: bool = False) -> Settings:
    """The example config with Discord IDs set and the greetings dir holding the test clip."""
    base = load_settings(EXAMPLE, {"DISCORD_TOKEN": TOKEN})
    greetings = tmp_path / "greetings"
    greetings.mkdir(exist_ok=True)
    shutil.copy(clip, greetings / clip.name)
    ids = {
        "guild_id": GUILD_ID,
        "voice_channel_id": VOICE_ID,
        "news_channel_id": NEWS_ID,
        "cycles_channel_id": CYCLES_ID,
        "voice_log_channel_id": VOICE_LOG_ID,
    }
    return base.model_copy(
        update={
            "discord": base.discord.model_copy(update=ids),
            "publishing": PublishingSettings(enabled=publishing),
            "voice": base.voice.model_copy(
                update={"greetings_dir": str(greetings), "ffmpeg_path": ffmpeg}
            ),
            "logging": base.logging.model_copy(
                update={"heartbeat_path": str(tmp_path / "heartbeat")}
            ),
        }
    )


# --- fakes at the Discord edge ----------------------------------------------------------------


class SenderArgs:
    """Replaces DiscordSender to capture the channel ids it is built with. Publishing is off in
    the harness, so the gate drops it at once (see test_build_services_wires_one_gate)."""

    def __init__(self, client: discord.Client, channel_ids: dict[ChannelKey, int]) -> None:
        self.channel_ids = dict(channel_ids)


class FakeConnector:
    """Replaces DiscordVoiceConnector. While `connected`, Amir is in `channel_id`: Warframe
    unless a connect said otherwise. `mid_connect` runs inside each connect before it is done,
    where discord.py dispatches his own voice update: after the join request, before the voice
    handshake."""

    def __init__(self, client: discord.Client) -> None:
        self.channel_id = VOICE_ID
        self.connected = False
        self.connects = 0
        self.connect_gate: asyncio.Event | None = None  # set: every connect waits on it
        self.mid_connect: Callable[[int], Awaitable[None]] | None = None
        self.disconnect_gate: asyncio.Event | None = None  # set: every disconnect waits on it
        self.disconnect_error: Exception | None = None  # raised by every disconnect while set
        self.disconnects = 0
        self.order: list[str] | None = None

    async def connect(
        self,
        channel_id: int,
        timeout: float,  # noqa: ASYNC109 - protocol signature
    ) -> None:
        self.connects += 1
        if self.mid_connect is not None:
            await self.mid_connect(channel_id)
        if self.connect_gate is not None:
            await self.connect_gate.wait()
        self.connected = True
        self.channel_id = channel_id

    async def disconnect(self) -> bool:
        self.disconnects += 1
        if self.order is not None:
            self.order.append("voice")
        if self.disconnect_gate is not None:
            await self.disconnect_gate.wait()
        if self.disconnect_error is not None:
            raise self.disconnect_error
        was, self.connected = self.connected, False
        return was

    def is_connected(self) -> bool:
        return self.connected

    def current_channel_id(self) -> int | None:
        return self.channel_id if self.connected else None

    def voice_client(self) -> None:
        return None


class FakePlayer:
    """Replaces DiscordPlayer."""

    def __init__(self, voice_client_provider, ffmpeg: str, audio_filter: str) -> None:
        self.played: list[Path] = []

    async def play(self, path: Path, listener_id: int | None) -> None:
        self.played.append(path)


class BlockingFetcher:
    """No network: every fetch is recorded and then waits until cancelled."""

    def __init__(self) -> None:
        self.urls: list[str] = []

    async def fetch(self, url: str, *, etag=None, last_modified=None):
        self.urls.append(url)
        await asyncio.Event().wait()


class FakeHttpSession:
    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.closed = False

    async def close(self) -> None:
        self.order.append("http")
        self.closed = True


class Spy:
    """Wraps a real coroutine method: counts the calls and calls through."""

    def __init__(self, target: Callable[..., Any]) -> None:
        self.target = target
        self.calls: list[tuple[Any, ...]] = []

    async def __call__(self, *args: Any) -> Any:
        self.calls.append(args)
        return await self.target(*args)


def spy(monkeypatch, obj: object, name: str) -> Spy:
    wrapper = Spy(getattr(obj, name))
    monkeypatch.setattr(obj, name, wrapper)
    return wrapper


def member(member_id: int, *, bot: bool = False) -> SimpleNamespace:
    return SimpleNamespace(id=member_id, bot=bot)


ME = SimpleNamespace(id=OWN_ID)  # Amir, as his guild's `me`


def state(channel_id: int | None, *, visible: bool = True) -> SimpleNamespace:
    """A discord.VoiceState as on_voice_state_update sees it. Amir's role may view its channel
    unless `visible` is False (the server's "private voice" is hidden from him)."""
    if channel_id is None:
        return SimpleNamespace(channel=None)
    perms = SimpleNamespace(view_channel=visible)

    def permissions_for(who: object) -> SimpleNamespace:
        assert who is ME, "only Amir's own permissions decide what he sees"
        return perms

    channel = SimpleNamespace(
        id=channel_id, guild=SimpleNamespace(me=ME), permissions_for=permissions_for
    )
    return SimpleNamespace(channel=channel)


def hidden(channel_id: int) -> SimpleNamespace:
    """A voice state in a channel hidden from Amir's role."""
    return state(channel_id, visible=False)


def voice_guild(channels: list[Any]) -> SimpleNamespace:
    """A guild from discord.py's cache; its channels are (id, members) pairs."""
    perms = SimpleNamespace(view_channel=True, connect=True, speak=True)
    return SimpleNamespace(
        me=object(),
        afk_channel=None,
        stage_channels=[],
        voice_channels=[
            SimpleNamespace(
                id=channel_id,
                position=position,
                members=members,
                user_limit=0,
                permissions_for=lambda who: perms,
            )
            for position, (channel_id, members) in enumerate(channels)
        ],
    )


async def settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


async def finished(name: str) -> None:
    """Wait for the supervised task `name` to end: it may write to the database, which takes
    longer than a few turns of the loop."""
    tasks = [t for t in asyncio.all_tasks() if t.get_name() == name]
    assert tasks, name
    await asyncio.wait_for(asyncio.gather(*tasks), 5)


# --- harness ----------------------------------------------------------------------------------


class Harness:
    def __init__(self, settings: Settings, db: Database, order: list[str]) -> None:
        self.settings = settings
        self.db = db
        self.order = order
        self.http = FakeHttpSession(order)
        self.fetcher = BlockingFetcher()
        self.members: list[Any] = []  # who is in the Warframe channel, as the census sees it
        self.elsewhere: list[Any] = []  # who is in the other voice channel
        self.registered: list[AmirBot] = []
        self.senders: list[SenderArgs] = []
        self.connectors: list[FakeConnector] = []
        self.kick_detectors: list[FakeKickDetector] = []
        self.bots: list[AmirBot] = []

    async def register(self, bot: AmirBot) -> None:
        self.registered.append(bot)

    def new_bot(self, **kwargs: Any) -> AmirBot:
        kwargs.setdefault("register_commands", self.register)
        kwargs.setdefault("voice_census", self.census)
        bot = AmirBot(
            self.settings,
            lambda client: build_services(client, self.settings, self.db, self.http, self.fetcher),
            **kwargs,
        )
        bot._connection.user = SimpleNamespace(id=OWN_ID)  # what discord.py sets on READY
        self.bots.append(bot)
        return bot

    @property
    def bot(self) -> AmirBot:
        return self.bots[0]

    @property
    def services(self) -> Services:
        return self.bot.services

    @property
    def sender(self) -> SenderArgs:
        return self.senders[0]

    @property
    def connector(self) -> FakeConnector:
        return self.connectors[0]

    @property
    def kicks(self) -> FakeKickDetector:
        return self.kick_detectors[0]

    def holds(self) -> VoiceHoldRepo:
        return VoiceHoldRepo(self.db)

    def census(self) -> VoiceCensus:
        """Warframe, then the other voice channel."""
        home = census_of(self.members, self.settings.discord.voice_channel_id)
        other = census_of(self.elsewhere, ELSEWHERE_ID)
        return VoiceCensus(home.channels + other.channels, afk_channel_id=None)


@pytest.fixture
async def harness(tmp_path, ffmpeg_exe, voice_clip, monkeypatch) -> AsyncIterator[Harness]:
    order: list[str] = []
    db = await Database.open(tmp_path / "amir.sqlite3")
    await db.migrate()
    real_close = db.close

    async def close_db() -> None:
        order.append("db")
        await real_close()

    monkeypatch.setattr(db, "close", close_db)
    h = Harness(make_settings(tmp_path, ffmpeg_exe, voice_clip), db, order)

    def sender(*args: Any) -> SenderArgs:
        h.senders.append(SenderArgs(*args))
        return h.senders[-1]

    def connector(*args: Any) -> FakeConnector:
        h.connectors.append(FakeConnector(*args))
        return h.connectors[-1]

    monkeypatch.setattr(client_module, "DiscordSender", sender)
    monkeypatch.setattr(client_module, "DiscordVoiceConnector", connector)

    def kick_detector(*args: Any) -> FakeKickDetector:
        h.kick_detectors.append(FakeKickDetector(*args))
        return h.kick_detectors[-1]

    monkeypatch.setattr(client_module, "DiscordPlayer", FakePlayer)
    monkeypatch.setattr(client_module, "DiscordKickDetector", kick_detector)
    monkeypatch.setattr(companion_module, "KICK_CHECK_DELAY_SECONDS", 0)  # no 2 s waits
    monkeypatch.setattr(companion_module, "AUDIT_REFRESH_DELAY_SECONDS", 0)
    h.new_bot()
    yield h
    for bot in h.bots:
        await TaskSupervisor.cancel_all(bot.services.supervisor)  # unpatched
        bot.services.session.cancel_disconnect()
    if "db" not in order:
        await real_close()


# --- intents and wiring -----------------------------------------------------------------------


def test_intents_minimal():
    assert build_intents().value == discord.Intents(guilds=True, voice_states=True).value


async def test_bot_uses_minimal_intents(harness):
    assert harness.bot.intents.value == build_intents().value


def _children(obj: object) -> Iterable[tuple[str, object]]:
    if isinstance(obj, dict):
        return [(repr(k), v) for k, v in obj.items()]
    if isinstance(obj, list | tuple | set | frozenset | deque):
        return [(str(i), v) for i, v in enumerate(obj)]
    if inspect.ismethod(obj):
        return [("__self__", obj.__self__)]
    if inspect.isfunction(obj):
        cells = []
        for i, cell in enumerate(obj.__closure__ or ()):
            try:
                cells.append((f"<cell {i}>", cell.cell_contents))
            except ValueError:  # an empty cell
                pass
        return cells
    if type(obj).__module__.startswith("amir.") and hasattr(obj, "__dict__"):
        return list(vars(obj).items())
    return []


def senders_outside_the_gate(services: Services) -> list[str]:
    """Paths from the services to a DiscordSender that do not pass through a GatedWriter."""
    found: list[str] = []
    seen: set[int] = set()
    queue: deque[tuple[object, str]] = deque([(services, "services")])
    while queue:
        obj, path = queue.popleft()
        if id(obj) in seen:
            continue
        seen.add(id(obj))
        if isinstance(obj, DiscordSender):
            found.append(path)
        elif not isinstance(obj, GatedWriter):
            queue.extend((child, f"{path}.{name}") for name, child in _children(obj))
    return found


@pytest.mark.parametrize("publishing", [False, True])
async def test_build_services_wires_one_gate(publishing, tmp_path, db, ffmpeg_exe, voice_clip):
    settings = make_settings(tmp_path, ffmpeg_exe, voice_clip, publishing=publishing)
    bot = AmirBot(
        settings,
        lambda client: build_services(client, settings, db, FakeHttpSession([]), BlockingFetcher()),
    )
    s = bot.services
    writer = s.writer

    assert isinstance(writer, GatedWriter)
    assert writer.enabled is publishing
    baro = s.worldstate._baro
    for component in (s.outbox, s.cycles, baro, s.announcer):
        assert component._writer is writer
    assert senders_outside_the_gate(s) == []
    if publishing:
        assert isinstance(writer._sender, DiscordSender)
        assert writer._sender._client is bot
        assert writer._sender._channel_ids == {
            ChannelKey.NEWS: NEWS_ID,
            ChannelKey.CYCLES: CYCLES_ID,
            ChannelKey.VOICE_LOG: VOICE_LOG_ID,
        }
    else:
        assert writer._sender is None
    await s.supervisor.cancel_all()


async def test_build_services_passes_the_settings(harness):
    s = harness.services
    v = harness.settings.voice
    assert s.companion._home_channel_id == VOICE_ID  # the connector takes a channel per connect
    assert harness.sender.channel_ids == {
        ChannelKey.NEWS: NEWS_ID,
        ChannelKey.CYCLES: CYCLES_ID,
        ChannelKey.VOICE_LOG: VOICE_LOG_ID,
    }
    assert s.greetings._queue.maxsize == v.queue_capacity
    assert s.greetings._expiry == v.greeting_expiry_seconds
    assert s.greetings._clock is s.companion._clock  # one monotonic clock for both
    assert s.companion._census == harness.bot.voice_census
    assert s.companion._settle_seconds == v.follow_settle_seconds
    stays = s.companion._stays
    assert stays._fresh == v.greeting_expiry_seconds  # caught within the greeting expiry
    assert stays._greet_on_rejoin is v.greet_on_rejoin
    assert stays._rejoin._window == v.rejoin_window_seconds
    assert stays._clock is stays._rejoin._clock is s.companion._clock
    assert s.session._grace_seconds == v.leave_grace_seconds
    assert s.session._connect_timeout == v.connect_timeout_seconds
    assert s.session._max_attempts == v.reconnect_max_attempts
    assert s.library._directory == Path(v.greetings_dir)
    # The audit log is the guild's; the hold lives in the bot's database.
    assert (harness.kicks.client, harness.kicks.guild_id) == (harness.bot, GUILD_ID)
    assert s.companion._kicks is harness.kicks
    assert isinstance(s.companion._holds, VoiceHoldRepo)
    assert s.companion._holds._db is harness.db
    assert s.news._role_id == harness.settings.discord.notification_role_id
    assert s.news._settings is harness.settings.news
    assert s.worldstate._settings is harness.settings.worldstate
    assert s.cycles._worlds == tuple(harness.settings.cycles.worlds)
    assert s.fetcher is harness.fetcher
    assert s.http is harness.http
    assert s.db is harness.db
    assert s.settings is harness.settings
    # One clock for the services and the admin reports.
    assert s.clock is s.cycles._clock is s.news._clock is s.worldstate._clock
    assert s.clock is harness.kicks.clock is s.companion._wall_clock


async def test_baro_role_ping_comes_from_settings(tmp_path, db, ffmpeg_exe, voice_clip):
    base = make_settings(tmp_path, ffmpeg_exe, voice_clip)
    settings = base.model_copy(
        update={
            "discord": base.discord.model_copy(update={"notification_role_id": 777}),
            "news": base.news.model_copy(update={"role_ping_categories": ["Baro Ki'Teer"]}),
        }
    )
    bot = AmirBot(
        settings,
        lambda client: build_services(client, settings, db, FakeHttpSession([]), BlockingFetcher()),
    )
    assert bot.services.worldstate._baro._role_ping_id == 777
    assert bot.services.news._role_id == 777


# --- on_ready ---------------------------------------------------------------------------------


async def test_on_ready_twice_starts_everything_once(harness, monkeypatch):
    s = harness.services
    started: list[str] = []
    real_start = s.supervisor.start

    def recording_start(name, factory):
        started.append(name)
        return real_start(name, factory)

    monkeypatch.setattr(s.supervisor, "start", recording_start)
    reconciles = spy(monkeypatch, s.outbox, "reconcile_inflight")
    scans = spy(monkeypatch, s.library, "scan")
    startups = spy(monkeypatch, s.companion, "on_startup")

    await harness.bot.on_ready()
    await harness.bot.on_ready()  # a gateway reconnect fires on_ready again
    await settle()

    assert Counter(started) == Counter([*WORKERS, "voice-startup"])
    assert len(reconciles.calls) == 1
    assert len(scans.calls) == 1
    assert len(startups.calls) == 1
    assert s.library.valid_count == 1  # the real scan validated the test clip
    running = Counter(t.get_name() for t in asyncio.all_tasks() if not t.done())
    assert {name: running[name] for name in WORKERS} == {name: 1 for name in WORKERS}
    assert harness.fetcher.urls  # the pollers really run (and never reach the network)


async def test_concurrent_on_ready_starts_everything_once(harness, monkeypatch):
    s = harness.services
    reconciles = spy(monkeypatch, s.outbox, "reconcile_inflight")
    scans = spy(monkeypatch, s.library, "scan")
    startups = spy(monkeypatch, s.companion, "on_startup")

    await asyncio.gather(harness.bot.on_ready(), harness.bot.on_ready())
    await settle()

    assert len(reconciles.calls) == 1
    assert len(scans.calls) == 1
    assert len(startups.calls) == 1
    running = Counter(t.get_name() for t in asyncio.all_tasks() if not t.done())
    assert {name: running[name] for name in WORKERS} == {name: 1 for name in WORKERS}


async def test_startup_join_never_delays_the_workers(harness):
    harness.members.append(member(MEMBER_ID))  # someone is already in Warframe
    harness.connector.connect_gate = asyncio.Event()  # and the voice connect hangs

    await asyncio.wait_for(harness.bot.on_ready(), 5)
    await settle()

    assert harness.connector.connects == 1  # the quiet startup join is under way...
    running = {t.get_name() for t in asyncio.all_tasks() if not t.done()}
    assert set(WORKERS) <= running  # ...and every worker already runs
    assert "voice-follow" in running
    harness.connector.connect_gate.set()
    await settle()
    assert harness.connector.connected
    assert len(harness.services.greetings) == 0  # quiet: nobody greeted


async def test_on_ready_survives_failed_reconcile_and_scan(harness, monkeypatch, caplog):
    s = harness.services

    async def broken() -> None:
        raise RuntimeError("database is locked")

    monkeypatch.setattr(s.outbox, "reconcile_inflight", broken)
    monkeypatch.setattr(s.library, "scan", broken)

    with caplog.at_level(logging.ERROR, logger="amir.bot.client"):
        await harness.bot.on_ready()
    await settle()

    running = {t.get_name() for t in asyncio.all_tasks() if not t.done()}
    assert set(WORKERS) <= running
    errors = [r for r in caplog.records if r.name == "amir.bot.client"]
    assert [r.levelno for r in errors] == [logging.ERROR, logging.ERROR]


async def test_on_ready_logs_the_greeting_scan_counts(harness, voice_clip, caplog):
    greetings = Path(harness.settings.voice.greetings_dir)  # already holds the test clip
    (greetings / "amir-greeting-02.qt").write_bytes(voice_clip.read_bytes()[:4000])  # truncated

    with caplog.at_level(logging.INFO, logger="amir.bot.client"):
        await harness.bot.on_ready()
    await settle()

    messages = [r.getMessage() for r in caplog.records if r.name == "amir.bot.client"]
    assert "greetings scanned: 1 valid, 1 invalid" in messages
    assert not any(str(greetings) in m or ".qt" in m for m in messages)  # counts only


# --- voice state updates ----------------------------------------------------------------------


async def test_voice_update_routed(harness, monkeypatch):
    events = spy(monkeypatch, harness.services.companion, "on_event")
    harness.members.append(member(MEMBER_ID))  # discord.py updates its cache before dispatching

    await harness.bot.on_voice_state_update(member(MEMBER_ID), state(None), state(VOICE_ID))
    await settle()

    assert events.calls == [(VoiceEvent(VoiceEventKind.JOIN, MEMBER_ID, VOICE_ID),)]
    assert harness.connector.connected  # the first arrival brings him...
    assert len(harness.services.greetings) == 1  # ...and his arrival queues their greeting
    await harness.bot.on_voice_state_update(member(OWN_ID, bot=True), state(None), state(VOICE_ID))
    await finished("voice-snapshot")
    assert harness.kicks.snapshots == 1  # his join, reported late here, notes the audit log
    assert len(harness.services.greetings) == 1  # and greets nobody twice


async def test_the_first_arrival_is_greeted_when_his_join_is_reported_mid_connect(harness):
    # discord.py dispatches Amir's own join right after his join request, before the voice
    # handshake is done: the audit log read it starts can finish while he is not yet connected.
    bot = harness.bot

    async def report_join(channel_id: int) -> None:
        await bot.on_voice_state_update(member(OWN_ID, bot=True), state(None), state(channel_id))
        await finished("voice-snapshot")  # the audit log answers before the handshake is done

    harness.connector.mid_connect = report_join
    harness.members.append(member(MEMBER_ID))

    await bot.on_voice_state_update(member(MEMBER_ID), state(None), state(VOICE_ID))
    await asyncio.wait_for(harness.services.companion.decided(), 5)

    assert harness.connector.connected and harness.connector.channel_id == VOICE_ID
    assert harness.kicks.snapshots == 1
    assert len(harness.services.greetings) == 1  # the first arrival of the evening is greeted


async def test_move_out_of_warframe_routed(harness, monkeypatch):
    events = spy(monkeypatch, harness.services.companion, "on_event")

    await harness.bot.on_voice_state_update(member(MEMBER_ID), state(VOICE_ID), state(ELSEWHERE_ID))

    assert events.calls == [(VoiceEvent(VoiceEventKind.MOVE, MEMBER_ID, ELSEWHERE_ID, VOICE_ID),)]


async def test_voice_changes_in_any_channel_reach_the_companion(harness, monkeypatch):
    events = spy(monkeypatch, harness.services.companion, "on_event")

    await harness.bot.on_voice_state_update(member(MEMBER_ID), state(None), state(ELSEWHERE_ID))
    await harness.bot.on_voice_state_update(member(MEMBER_ID), state(VOICE_ID), state(ELSEWHERE_ID))

    assert events.calls == [
        (VoiceEvent(VoiceEventKind.JOIN, MEMBER_ID, ELSEWHERE_ID),),
        (VoiceEvent(VoiceEventKind.MOVE, MEMBER_ID, ELSEWHERE_ID, VOICE_ID),),
    ]


@pytest.mark.parametrize(
    ("before", "after", "seen"),
    [
        (state(None), hidden(PRIVATE_ID), None),
        (hidden(PRIVATE_ID), state(None), None),
        (hidden(PRIVATE_ID), hidden(SECRET_ID), None),
        (state(VOICE_ID), hidden(PRIVATE_ID), VoiceEventKind.LEAVE),
        (hidden(PRIVATE_ID), state(VOICE_ID), VoiceEventKind.JOIN),
    ],
    ids=["joins it", "leaves it", "hidden to hidden", "visible to hidden", "hidden to visible"],
)
async def test_a_channel_hidden_from_amir_never_reaches_the_log(
    before, after, seen, harness, monkeypatch
):
    # Members' privacy: Discord may still send voice states for a channel Amir's role can't
    # view. To him it is no channel at all, so a move from or to it is a leave or a join.
    events = spy(monkeypatch, harness.services.companion, "on_event")
    mention, warframe = f"<@{MEMBER_ID}>", f"<#{VOICE_ID}>"

    await harness.bot.on_voice_state_update(member(MEMBER_ID), before, after)
    await settle()

    if seen is None:
        assert events.calls == []
        assert harness.services.announcer._pending == []
    elif seen is VoiceEventKind.LEAVE:
        assert events.calls == [(VoiceEvent(VoiceEventKind.LEAVE, MEMBER_ID, VOICE_ID),)]
        assert harness.services.announcer._pending == [persona.voice_leave_line(mention, warframe)]
    else:
        assert events.calls == [(VoiceEvent(VoiceEventKind.JOIN, MEMBER_ID, VOICE_ID),)]
        assert harness.services.announcer._pending == [persona.voice_join_line(mention, warframe)]
    pending = "\n".join(harness.services.announcer._pending)
    assert str(PRIVATE_ID) not in pending and str(SECRET_ID) not in pending
    assert harness.connector.connects == 0  # nobody he could sit with


async def test_no_channel_is_seen_while_amir_is_unknown_in_his_guild(harness, monkeypatch):
    # discord.py types guild.me as optional: without it, his view can't be read, so nothing is
    # taken for seen.
    events = spy(monkeypatch, harness.services.companion, "on_event")
    unknown = SimpleNamespace(channel=SimpleNamespace(id=VOICE_ID, guild=SimpleNamespace(me=None)))

    await harness.bot.on_voice_state_update(member(MEMBER_ID), state(None), unknown)
    await settle()

    assert events.calls == []
    assert harness.services.announcer._pending == []


@pytest.mark.parametrize(
    ("before", "after"),
    [(hidden(PRIVATE_ID), state(None)), (state(VOICE_ID), state(None))],
    ids=["from a hidden channel", "from a channel he sees"],
)
async def test_any_disconnect_refreshes_the_audit_snapshot_even_unseen(
    before, after, harness, monkeypatch
):
    # A moderator's disconnect in a channel hidden from Amir adds to the audit log all the
    # same: re-read it, so it is never taken for a kick of Amir later. Only the line is unseen.
    events = spy(monkeypatch, harness.services.companion, "on_event")
    harness.connector.connected = True  # Amir is in voice

    await harness.bot.on_voice_state_update(member(MEMBER_ID), before, after)
    await finished("voice-audit-refresh")

    assert harness.kicks.snapshots == 1
    assert len(events.calls) == (0 if before.channel.id == PRIVATE_ID else 1)


@pytest.mark.parametrize(
    ("who", "before", "after"),
    [
        (member(MEMBER_ID), ELSEWHERE_ID, ELSEWHERE_ID),  # mute/deafen elsewhere
        (member(MEMBER_ID), VOICE_ID, VOICE_ID),  # mute/deafen inside Warframe
        (member(5001, bot=True), None, VOICE_ID),  # another bot
        (member(OWN_ID, bot=True), None, VOICE_ID),  # Amir himself arriving
    ],
)
async def test_other_updates_are_ignored(who, before, after, harness, monkeypatch):
    events = spy(monkeypatch, harness.services.companion, "on_event")
    rejoins = spy(monkeypatch, harness.services.companion, "on_own_disconnect")

    await harness.bot.on_voice_state_update(who, state(before), state(after))
    await settle()

    assert events.calls == []
    assert rejoins.calls == []


async def test_own_disconnect_routed(harness, monkeypatch):
    companion = harness.services.companion
    rejoins = spy(monkeypatch, companion, "on_own_disconnect")
    events = spy(monkeypatch, companion, "on_event")
    harness.members.append(member(MEMBER_ID))  # a human is still in Warframe

    await harness.bot.on_voice_state_update(member(OWN_ID, bot=True), state(VOICE_ID), state(None))
    await settle()

    assert len(rejoins.calls) == 1
    assert harness.kicks.asked == 1  # not a kick: a network drop, say
    assert events.calls == []
    assert harness.connector.connected  # back in Warframe...
    assert len(harness.services.greetings) == 0  # ...quietly: no greeting
    assert harness.services.announcer._pending == []  # and no voice-log line
    assert await harness.holds().get() is None


async def test_own_kick_keeps_amir_out_across_a_restart(harness, monkeypatch):
    harness.members.append(member(MEMBER_ID))
    harness.connector.connected = True  # in Warframe with the group
    harness.kicks.kicked = True  # the audit log shows a moderator's disconnect

    harness.connector.connected = False  # Discord drops him...
    await harness.bot.on_voice_state_update(member(OWN_ID, bot=True), state(VOICE_ID), state(None))
    await finished("voice-rejoin")

    assert harness.connector.connects == 0  # ...and he stays out
    hold = await harness.holds().get()
    assert hold is not None and hold.reason == "kicked"
    assert harness.services.companion.hold == hold

    restarted = harness.new_bot()  # a new process over the same database
    startups = spy(monkeypatch, restarted.services.companion, "on_startup")

    async def sync(*, guild=None):
        return []

    monkeypatch.setattr(restarted.tree, "sync", sync)
    await restarted.setup_hook()  # reads the hold back before the gateway connects
    await restarted.on_ready()
    await settle()

    assert len(startups.calls) == 1
    assert restarted.services.companion.hold == hold
    assert harness.connectors[1].connects == 0  # people are present, yet he stays out
    assert harness.kick_detectors[1].snapshots == 1


def record_starts(monkeypatch, supervisor: TaskSupervisor) -> list[str]:
    """The name of every task the supervisor is asked to start."""
    started: list[str] = []
    real_start = supervisor.start

    def start(name, factory, *, routine=False):
        started.append(name)
        return real_start(name, factory, routine=routine)

    monkeypatch.setattr(supervisor, "start", start)
    return started


async def test_following_the_crowd_is_never_a_kick_or_a_drag(harness, monkeypatch):
    s = harness.services
    started = record_starts(monkeypatch, s.supervisor)
    arrivals = spy(monkeypatch, s.companion, "on_own_connect")
    bot = harness.bot
    harness.members.append(member(MEMBER_ID))
    await s.session.ensure_connected(VOICE_ID)  # in Warframe with them
    harness.connector.disconnect_gate = asyncio.Event()  # his leave takes a moment

    # They move to the other channel: Warframe empties, so he follows them at once.
    harness.members.clear()
    harness.elsewhere.append(member(MEMBER_ID))
    await bot.on_voice_state_update(member(MEMBER_ID), state(VOICE_ID), state(ELSEWHERE_ID))
    await settle()
    assert s.session.expecting_disconnect  # his own leave is under way...
    await bot.on_voice_state_update(member(OWN_ID, bot=True), state(VOICE_ID), state(None))
    harness.connector.disconnect_gate.set()  # ...and Discord reported it meanwhile
    await settle()
    assert harness.connector.connected and harness.connector.channel_id == ELSEWHERE_ID
    await bot.on_voice_state_update(member(OWN_ID, bot=True), state(None), state(ELSEWHERE_ID))
    await finished("voice-snapshot")

    assert "voice-rejoin" not in started
    assert "voice-move" not in started
    assert started.count("voice-snapshot") == 1
    assert arrivals.calls == [(ELSEWHERE_ID,)]
    assert harness.kicks.asked == 0  # no kick check
    assert s.companion.hold is None and await harness.holds().get() is None
    assert s.companion.pin is None
    assert not s.session.expecting_disconnect


async def test_a_failed_leave_ends_his_change_of_channel(harness, monkeypatch, caplog):
    s = harness.services
    started = record_starts(monkeypatch, s.supervisor)
    harness.members.append(member(MEMBER_ID))
    await s.session.ensure_connected(VOICE_ID)
    harness.connector.disconnect_error = RuntimeError("voice websocket already closed")

    harness.members.clear()
    harness.elsewhere.append(member(MEMBER_ID))
    with caplog.at_level(logging.WARNING, logger="amir.voice.session"):
        await harness.bot.on_voice_state_update(
            member(MEMBER_ID), state(VOICE_ID), state(ELSEWHERE_ID)
        )
        await settle()

    # No join, so no leave of the connector's own that the bot would take for a kick.
    assert (harness.connector.disconnects, harness.connector.connects) == (1, 1)
    assert harness.connector.connected and harness.connector.channel_id == VOICE_ID
    assert "voice-rejoin" not in started
    assert harness.kicks.asked == 0
    assert s.companion.hold is None and s.companion.pin is None
    assert "voice disconnect failed: RuntimeError" in caplog.messages


async def test_a_drag_starts_voice_move_with_the_new_channel(harness, monkeypatch):
    companion = harness.services.companion
    rejoins = spy(monkeypatch, companion, "on_own_disconnect")
    moves = spy(monkeypatch, companion, "on_own_move")
    harness.members.append(member(MEMBER_ID))
    harness.elsewhere.append(member(4002))
    await harness.services.session.ensure_connected(VOICE_ID)
    harness.connector.channel_id = ELSEWHERE_ID  # a moderator drags him to the other channel

    await harness.bot.on_voice_state_update(
        member(OWN_ID, bot=True), state(VOICE_ID), state(ELSEWHERE_ID)
    )
    await finished("voice-move")
    await settle()

    assert moves.calls == [(ELSEWHERE_ID,)]
    assert rejoins.calls == []
    assert harness.kicks.asked == 0  # a drag is deliberate, whoever did it
    assert companion.pin is not None and companion.pin.channel_id == ELSEWHERE_ID
    assert companion.hold is None and await harness.holds().get() is None
    assert harness.connector.connected  # he stays where they put him
    assert harness.connector.channel_id == ELSEWHERE_ID
    assert harness.connector.connects == 1  # and was not moved back


@pytest.mark.parametrize("channel", [VOICE_ID, ELSEWHERE_ID], ids=["warframe", "elsewhere"])
async def test_own_arrival_reads_the_audit_log(channel, harness, monkeypatch):
    rejoins = spy(monkeypatch, harness.services.companion, "on_own_disconnect")
    arrivals = spy(monkeypatch, harness.services.companion, "on_own_connect")

    await harness.bot.on_voice_state_update(member(OWN_ID, bot=True), state(None), state(channel))
    await finished("voice-snapshot")

    assert arrivals.calls == [(channel,)]
    assert harness.kicks.snapshots == 1  # what the audit log holds as he joins
    assert harness.kicks.asked == 0
    assert rejoins.calls == []


async def test_own_disconnect_from_any_channel_is_checked_for_a_kick(harness, monkeypatch):
    rejoins = spy(monkeypatch, harness.services.companion, "on_own_disconnect")

    await harness.bot.on_voice_state_update(
        member(OWN_ID, bot=True), state(ELSEWHERE_ID), state(None)
    )
    await finished("voice-rejoin")

    assert len(rejoins.calls) == 1
    assert harness.kicks.asked == 1


@pytest.mark.parametrize("channel", [VOICE_ID, ELSEWHERE_ID], ids=["warframe", "elsewhere"])
async def test_own_mute_or_deafen_does_nothing(channel, harness):
    await harness.bot.on_voice_state_update(
        member(OWN_ID, bot=True), state(channel), state(channel)
    )
    await settle()

    assert (harness.kicks.snapshots, harness.kicks.asked) == (0, 0)
    assert await harness.holds().get() is None
    assert harness.services.companion.pin is None


async def test_own_disconnect_is_not_awaited_inline(harness):
    harness.members.append(member(MEMBER_ID))
    harness.connector.connect_gate = asyncio.Event()  # the rejoin hangs

    await asyncio.wait_for(
        harness.bot.on_voice_state_update(member(OWN_ID, bot=True), state(VOICE_ID), state(None)),
        1,
    )
    await settle()

    assert harness.connector.connects == 1
    # The kick check ran in voice-rejoin; the rejoin itself hangs in the follow task.
    assert "voice-follow" in {t.get_name() for t in asyncio.all_tasks() if not t.done()}
    harness.connector.connect_gate.set()


async def test_deliberate_disconnect_is_not_rejoined(harness, monkeypatch):
    rejoins = spy(monkeypatch, harness.services.companion, "on_own_disconnect")
    harness.members.append(member(MEMBER_ID))
    harness.connector.connected = True
    harness.connector.disconnect_gate = asyncio.Event()
    leaving = asyncio.create_task(harness.services.session.disconnect())
    await settle()
    assert harness.services.session.expecting_disconnect

    await harness.bot.on_voice_state_update(member(OWN_ID, bot=True), state(VOICE_ID), state(None))
    harness.connector.disconnect_gate.set()
    await leaving
    await settle()

    assert rejoins.calls == []
    assert not harness.connector.connected
    assert harness.kicks.asked == 0
    assert await harness.holds().get() is None


@pytest.mark.parametrize("before", [VOICE_ID, ELSEWHERE_ID], ids=["warframe", "elsewhere"])
@pytest.mark.parametrize("is_bot", [False, True], ids=["human", "bot"])
async def test_another_members_disconnect_refreshes_the_audit_snapshot(
    before, is_bot, harness, caplog
):
    # A moderator may have disconnected them: their audit entry must not read as a later kick.
    harness.connector.connected = True  # Amir is in voice
    with caplog.at_level(logging.DEBUG, logger="amir.bot.tasks"):
        await harness.bot.on_voice_state_update(
            member(MEMBER_ID, bot=is_bot), state(before), state(None)
        )
        await finished("voice-audit-refresh")

    assert harness.kicks.snapshots == 1
    assert harness.kicks.asked == 0
    assert (logging.DEBUG, "task voice-audit-refresh finished") in [
        (r.levelno, r.getMessage()) for r in caplog.records
    ]


@pytest.mark.parametrize("in_voice", [True, False], ids=["moving", "joining"])
async def test_a_disconnect_while_he_connects_refreshes_the_audit_snapshot(in_voice, harness):
    # Every follow move connects. A moderator's disconnect of someone else meanwhile must not
    # read as a kick of Amir later.
    s = harness.services
    harness.connector.connected = in_voice  # in Warframe, or out of voice
    harness.connector.connect_gate = asyncio.Event()  # his connect takes a moment
    connecting = asyncio.create_task(s.session.ensure_connected(ELSEWHERE_ID))
    await settle()
    assert s.session.state == "connecting"

    await harness.bot.on_voice_state_update(member(MEMBER_ID), state(VOICE_ID), state(None))
    await finished("voice-audit-refresh")

    assert harness.kicks.snapshots == 1
    assert harness.kicks.asked == 0
    harness.connector.connect_gate.set()
    assert await asyncio.wait_for(connecting, 5)


@pytest.mark.parametrize(
    ("who", "before", "after", "in_voice"),
    [
        (member(MEMBER_ID), state(VOICE_ID), state(None), False),  # Amir is out: his join reads
        (member(MEMBER_ID), state(VOICE_ID), state(ELSEWHERE_ID), True),  # a move: no disconnect
        (member(MEMBER_ID), state(VOICE_ID), hidden(PRIVATE_ID), True),  # a move all the same
        (member(MEMBER_ID), state(None), state(VOICE_ID), True),  # an arrival
        (member(OWN_ID, bot=True), state(VOICE_ID), state(None), True),  # the kick check's job
    ],
    ids=["amir-out", "move", "move-out-of-sight", "arrival", "own"],
)
async def test_other_updates_do_not_refresh_the_audit_snapshot(
    who, before, after, in_voice, harness, monkeypatch
):
    rejoins = spy(monkeypatch, harness.services.companion, "on_own_disconnect")
    harness.connector.connected = in_voice
    await harness.bot.on_voice_state_update(who, before, after)
    await settle()

    running = {t.get_name() for t in asyncio.all_tasks() if not t.done()}
    assert "voice-audit-refresh" not in running
    assert harness.kicks.snapshots == 0
    assert len(rejoins.calls) == (1 if who.id == OWN_ID else 0)


@pytest.mark.parametrize(
    ("before", "after", "task"),
    [(None, VOICE_ID, "voice-snapshot"), (VOICE_ID, ELSEWHERE_ID, "voice-move")],
)
async def test_routine_voice_tasks_finish_quietly(before, after, task, harness, caplog):
    harness.connector.connected = True
    with caplog.at_level(logging.DEBUG, logger="amir.bot.tasks"):
        await harness.bot.on_voice_state_update(
            member(OWN_ID, bot=True), state(before), state(after)
        )
        await finished(task)
    finished_lines = [
        (r.levelno, r.getMessage()) for r in caplog.records if "finished" in r.getMessage()
    ]
    assert finished_lines == [(logging.DEBUG, f"task {task} finished")]  # not INFO on every join


# --- the voice census -------------------------------------------------------------------------


async def test_voice_census_reads_the_guild_from_the_cache(harness, monkeypatch):
    bot = harness.new_bot(voice_census=None)
    asked: list[int] = []
    members = [member(MEMBER_ID), member(OWN_ID, bot=True), member(4002)]
    guild = voice_guild([(ELSEWHERE_ID, [member(4003)]), (VOICE_ID, members)])

    def get_guild(guild_id: int):
        asked.append(guild_id)
        return guild

    monkeypatch.setattr(bot, "get_guild", get_guild)
    census = bot.voice_census()
    assert asked == [GUILD_ID]
    assert census.humans_in(VOICE_ID) == frozenset({MEMBER_ID, 4002})  # never Amir himself
    assert census.humans_in(ELSEWHERE_ID) == frozenset({4003})
    assert bot.services.companion.humans_present() == 3  # everyone he could be with


async def test_voice_census_is_empty_when_the_guild_is_unresolved(harness, monkeypatch):
    bot = harness.new_bot(voice_census=None)
    monkeypatch.setattr(bot, "get_guild", lambda guild_id: None)
    assert bot.voice_census() == VoiceCensus.EMPTY
    assert bot.services.companion.humans_present() == 0


# --- setup_hook -------------------------------------------------------------------------------


class FakeResponse:
    status = 503
    reason = "Service Unavailable"


async def test_command_sync_failure_is_non_fatal(harness, monkeypatch, caplog):
    synced: list[int] = []

    async def failing_sync(*, guild=None):
        synced.append(guild.id)
        raise discord.HTTPException(FakeResponse(), "sync failed")

    monkeypatch.setattr(harness.bot.tree, "sync", failing_sync)
    with caplog.at_level(logging.ERROR, logger="amir.bot.client"):
        await harness.bot.setup_hook()  # returns normally

    assert harness.registered == [harness.bot]
    assert synced == [GUILD_ID]
    assert [r.levelno for r in caplog.records if r.name == "amir.bot.client"] == [logging.ERROR]


async def test_successful_command_sync_is_logged(harness, monkeypatch, caplog):
    async def sync(*, guild=None):
        return []

    monkeypatch.setattr(harness.bot.tree, "sync", sync)
    with caplog.at_level(logging.INFO, logger="amir.bot.client"):
        await harness.bot.setup_hook()

    records = [r for r in caplog.records if r.name == "amir.bot.client"]
    assert [(r.levelno, r.getMessage()) for r in records] == [
        (logging.INFO, "slash commands synced")
    ]


async def test_setup_hook_reads_back_a_hold_before_anything_else(harness, monkeypatch):
    hold = VoiceHold(datetime.now(UTC), "moved")
    await harness.holds().save(hold)  # left by an earlier run
    seen: list[Any] = []

    async def register(bot: AmirBot) -> None:
        seen.append(bot.services.companion.hold)

    async def sync(*, guild=None):
        return []

    bot = harness.new_bot(register_commands=register)
    monkeypatch.setattr(bot.tree, "sync", sync)
    assert bot.services.companion.hold is None
    await bot.setup_hook()

    assert seen == [hold]  # read before the commands, long before the gateway connects


async def test_setup_hook_syncs_for_the_guild_only(harness, monkeypatch):
    bot = harness.new_bot(register_commands=None)  # registration: test_commands.py
    synced: list[Any] = []

    async def sync(*, guild=None):
        synced.append(guild)
        return []

    monkeypatch.setattr(bot.tree, "sync", sync)
    await bot.setup_hook()

    assert [g.id for g in synced] == [GUILD_ID]
    assert harness.registered == []


# --- heartbeat --------------------------------------------------------------------------------


async def test_heartbeat_written_while_ready(harness, monkeypatch):
    path = Path(harness.settings.logging.heartbeat_path)
    monkeypatch.setattr(harness.bot, "is_ready", lambda: True)
    beat = asyncio.create_task(harness.bot._heartbeat())
    try:
        for _ in range(200):
            if await asyncio.to_thread(path.exists):
                break
            await asyncio.sleep(0.01)
        assert health.check(path, datetime.now(UTC)) == 0
    finally:
        beat.cancel()


async def test_no_heartbeat_before_ready(harness, monkeypatch):
    path = Path(harness.settings.logging.heartbeat_path)
    asked = asyncio.Event()

    def is_ready() -> bool:
        asked.set()
        return False

    monkeypatch.setattr(harness.bot, "is_ready", is_ready)
    beat = asyncio.create_task(harness.bot._heartbeat())
    await asyncio.wait_for(asked.wait(), 1)
    await settle()
    beat.cancel()
    assert not await asyncio.to_thread(path.exists)


# --- close ------------------------------------------------------------------------------------


def record_close_steps(harness: Harness, monkeypatch) -> list[str]:
    order = harness.order
    supervisor = harness.services.supervisor
    real_cancel_all = supervisor.cancel_all

    async def cancel_all() -> None:
        order.append("supervisor")
        await real_cancel_all()

    monkeypatch.setattr(supervisor, "cancel_all", cancel_all)
    harness.connector.order = order
    real_client_close = discord.Client.close

    async def client_close(self) -> None:
        order.append("client")
        await real_client_close(self)

    monkeypatch.setattr(discord.Client, "close", client_close)
    return order


async def test_close_order(harness, monkeypatch):
    order = record_close_steps(harness, monkeypatch)
    await harness.bot.on_ready()
    await settle()

    await harness.bot.close()

    assert order == ["supervisor", "voice", "client", "http", "db"]
    assert not [t for t in asyncio.all_tasks() if t.get_name() in WORKERS and not t.done()]
    assert harness.bot.is_closed()


async def test_close_is_idempotent(harness, monkeypatch):
    order = record_close_steps(harness, monkeypatch)

    await asyncio.gather(harness.bot.close(), harness.bot.close())  # e.g. SIGTERM twice
    await harness.bot.close()  # and the entry point's final close

    assert order == ["supervisor", "voice", "client", "http", "db"]


async def test_close_runs_every_step_despite_failures(harness, monkeypatch, caplog):
    order = harness.order
    monkeypatch.setattr(client_module, "VOICE_DISCONNECT_TIMEOUT", 0.05)

    async def cancel_all() -> None:
        order.append("supervisor")
        raise RuntimeError("cancel failed")

    async def client_close(self) -> None:
        order.append("client")
        raise RuntimeError("gateway close failed")

    async def http_close() -> None:
        order.append("http")
        raise RuntimeError("http close failed")

    monkeypatch.setattr(harness.services.supervisor, "cancel_all", cancel_all)
    monkeypatch.setattr(discord.Client, "close", client_close)
    monkeypatch.setattr(harness.http, "close", http_close)
    harness.connector.order = order
    harness.connector.disconnect_gate = asyncio.Event()  # leaving voice hangs

    with caplog.at_level(logging.WARNING, logger="amir.bot.client"):
        await asyncio.wait_for(harness.bot.close(), 5)

    assert order == ["supervisor", "voice", "client", "http", "db"]
    levels = [r.levelno for r in caplog.records if r.name == "amir.bot.client"]
    assert levels.count(logging.ERROR) == 3  # supervisor, client, http
    assert levels.count(logging.WARNING) == 1  # the voice timeout


async def test_close_logs_every_failed_step(harness, monkeypatch, caplog):
    order = record_close_steps(harness, monkeypatch)
    heartbeat = Path(harness.settings.logging.heartbeat_path)
    await asyncio.to_thread(heartbeat.mkdir)  # a directory cannot be unlinked

    async def broken_cancel_background() -> None:
        order.append("companion")
        raise RuntimeError("companion stuck")

    async def broken_disconnect() -> None:
        order.append("voice")
        raise RuntimeError("voice gone")

    async def broken_db_close() -> None:
        order.append("db")
        await harness.db.conn.close()  # still release the connection
        raise RuntimeError("database close failed")

    monkeypatch.setattr(harness.services.companion, "cancel_background", broken_cancel_background)
    monkeypatch.setattr(harness.services.session, "disconnect", broken_disconnect)
    monkeypatch.setattr(harness.db, "close", broken_db_close)

    with caplog.at_level(logging.ERROR, logger="amir.bot.client"):
        await harness.bot.close()

    assert order == ["companion", "supervisor", "voice", "client", "http", "db"]
    errors = [r.exc_info[1] for r in caplog.records if r.name == "amir.bot.client"]
    assert [type(e) for e in errors] == [
        RuntimeError,
        IsADirectoryError,
        RuntimeError,
        RuntimeError,
    ]
    assert [str(e) for e in errors if isinstance(e, RuntimeError)] == [
        "companion stuck",
        "voice gone",
        "database close failed",
    ]


async def test_close_never_lets_a_follow_waiting_on_a_kick_check_connect(harness):
    """A kick check is pending, and the follow task, woken by a rejoin, waits for its answer.
    Cancelling the check must not release it into a connect while shutting down."""
    bot = harness.bot
    harness.members.append(member(MEMBER_ID))
    harness.kicks.gate = asyncio.Event()  # the audit log answer never comes
    await bot.on_voice_state_update(member(OWN_ID, bot=True), state(VOICE_ID), state(None))
    await asyncio.wait_for(harness.kicks.asking.wait(), 1)
    await bot.on_voice_state_update(member(MEMBER_ID), state(VOICE_ID), state(None))
    await bot.on_voice_state_update(member(MEMBER_ID), state(None), state(VOICE_ID))
    await settle()
    assert "voice-follow" in {t.get_name() for t in asyncio.all_tasks() if not t.done()}

    await asyncio.wait_for(bot.close(), 5)

    assert harness.connector.connects == 0
    assert not [t for t in asyncio.all_tasks() if t.get_name().startswith("voice") and not t.done()]


async def gone(name: str) -> None:
    """Wait until no task called `name` is left."""
    for _ in range(500):
        if not [t for t in asyncio.all_tasks() if t.get_name() == name and not t.done()]:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"{name} still running")


async def test_a_failing_hold_save_neither_crashes_the_check_nor_outlives_a_release(
    harness, monkeypatch, caplog
):
    monkeypatch.setattr(companion_module, "HOLD_SAVE_RETRY_FIRST_SECONDS", 0.01)
    monkeypatch.setattr(companion_module, "HOLD_SAVE_RETRY_MAX_SECONDS", 0.01)
    harness.members.append(member(MEMBER_ID))
    harness.connector.connected = True
    harness.kicks.kicked = True
    holds = harness.services.companion._holds
    saves: list[VoiceHold] = []

    async def broken_save(hold: VoiceHold) -> None:  # the /data volume is full, say
        saves.append(hold)
        raise OSError("disk I/O error")

    monkeypatch.setattr(holds, "save", broken_save)
    bot = harness.bot
    with caplog.at_level(logging.WARNING):
        harness.connector.connected = False
        await bot.on_voice_state_update(member(OWN_ID, bot=True), state(VOICE_ID), state(None))
        await gone("voice-rejoin")  # decided and done: no crash, no restart
        for _ in range(500):
            if len(saves) >= 3:
                break
            await asyncio.sleep(0.01)
        assert len(saves) >= 3  # the companion keeps retrying meanwhile

        assert await harness.services.companion.release_hold() is True  # the owner
        await gone("voice-hold-save")
        await gone("voice-follow")
        assert harness.connector.connects == 1  # lifted means back
        attempts = len(saves)

        # Later, an accidental drop: a new voice-rejoin starts and Amir comes back.
        harness.kicks.kicked = False
        harness.connector.connected = False
        await bot.on_voice_state_update(member(OWN_ID, bot=True), state(VOICE_ID), state(None))
        await gone("voice-rejoin")
        await gone("voice-follow")

    assert harness.kicks.asked == 2  # the kick was checked once, the accident once
    assert len(saves) == attempts  # no save after the release
    assert harness.services.companion.hold is None
    assert await harness.holds().get() is None
    assert harness.connector.connects == 2 and harness.connector.connected
    assert not [
        r for r in caplog.records if r.levelno >= logging.ERROR
    ]  # the supervisor saw nothing


async def test_close_cancels_a_pending_follow(harness, monkeypatch, caplog):
    """A member leaves and rejoins: the companion's follow task, outside the supervisor, starts
    to bring Amir to them. Its connect hangs while the bot shuts down."""
    monkeypatch.setattr(client_module, "VOICE_DISCONNECT_TIMEOUT", 0.3)
    bot = harness.bot
    harness.members.append(member(MEMBER_ID))
    await bot.on_voice_state_update(member(MEMBER_ID), state(VOICE_ID), state(None))
    harness.connector.connect_gate = asyncio.Event()
    await bot.on_voice_state_update(member(MEMBER_ID), state(None), state(VOICE_ID))
    await settle()
    assert "voice-follow" in {t.get_name() for t in asyncio.all_tasks() if not t.done()}

    with caplog.at_level(logging.WARNING, logger="amir.bot.client"):
        await asyncio.wait_for(bot.close(), 0.25)  # well inside the voice timeout

    assert not caplog.records  # leaving voice did not wait for the lock
    assert not [t for t in asyncio.all_tasks() if t.get_name().startswith("voice") and not t.done()]
    harness.connector.connect_gate.set()
    await settle()
    assert not harness.connector.connected  # the cancelled join never lands after close


async def test_voice_updates_are_ignored_once_closing(harness, monkeypatch):
    events = spy(monkeypatch, harness.services.companion, "on_event")
    rejoins = spy(monkeypatch, harness.services.companion, "on_own_disconnect")
    harness.members.append(member(MEMBER_ID))
    harness.connector.disconnect_gate = asyncio.Event()  # hold the shutdown inside "voice"
    closing = asyncio.create_task(harness.bot.close())
    await settle()

    await harness.bot.on_voice_state_update(member(MEMBER_ID), state(None), state(VOICE_ID))
    await harness.bot.on_voice_state_update(member(OWN_ID, bot=True), state(VOICE_ID), state(None))
    harness.connector.disconnect_gate.set()
    await closing

    assert events.calls == []
    assert rejoins.calls == []
    assert len(harness.services.greetings) == 0


async def test_close_removes_the_heartbeat(harness):
    path = Path(harness.settings.logging.heartbeat_path)
    await asyncio.to_thread(health.write_heartbeat, path, datetime.now(UTC))
    assert health.check(path, datetime.now(UTC)) == 0

    await harness.bot.close()

    assert not await asyncio.to_thread(path.exists)
    assert health.check(path, datetime.now(UTC)) == 1  # unhealthy at once, not 120 s later


async def test_a_heartbeat_write_in_flight_at_close_does_not_outlive_it(harness, monkeypatch):
    """Cancelling the heartbeat task cannot stop a write already running in its thread."""
    path = Path(harness.settings.logging.heartbeat_path)
    monkeypatch.setattr(harness.bot, "is_ready", lambda: True)
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    def slow_write(target: Path, now: datetime) -> None:
        started.set()
        try:
            release.wait(5)
            health.write_heartbeat(target, now)
        finally:
            finished.set()

    monkeypatch.setattr(client_module, "write_heartbeat", slow_write)
    harness.services.supervisor.start("heartbeat", harness.bot._heartbeat)
    assert await asyncio.to_thread(started.wait, 5)  # the write is under way in its thread
    closing = asyncio.create_task(harness.bot.close())
    await settle()  # the heartbeat task is cancelled, its thread still writing
    release.set()
    await asyncio.wait_for(closing, 5)
    assert await asyncio.to_thread(finished.wait, 5)

    assert not await asyncio.to_thread(path.exists)


async def test_no_heartbeat_is_written_once_closing(harness, monkeypatch):
    path = Path(harness.settings.logging.heartbeat_path)
    monkeypatch.setattr(client_module, "HEARTBEAT_SECONDS", 0.01)
    asked = 0
    beaten = asyncio.Event()

    def is_ready() -> bool:
        nonlocal asked
        asked += 1
        if asked == 3:  # two beats have been attempted, each fully, since closing began
            beaten.set()
        return True

    monkeypatch.setattr(harness.bot, "is_ready", is_ready)
    harness.connector.order = harness.order
    harness.connector.disconnect_gate = asyncio.Event()  # hold the shutdown inside "voice"
    closing = asyncio.create_task(harness.bot.close())
    for _ in range(500):
        if "voice" in harness.order:
            break
        await asyncio.sleep(0.01)
    assert "voice" in harness.order  # past the heartbeat removal, not yet closed
    beat = asyncio.create_task(harness.bot._heartbeat())  # e.g. one the supervisor missed
    try:
        await asyncio.wait_for(beaten.wait(), 5)
        assert not await asyncio.to_thread(path.exists)
    finally:
        beat.cancel()
        harness.connector.disconnect_gate.set()
        await closing
    assert not await asyncio.to_thread(path.exists)
