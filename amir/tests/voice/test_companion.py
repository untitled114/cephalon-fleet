import asyncio
import contextlib
import dataclasses
import functools
import gc
import logging
import shutil
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import discord
import pytest

from amir import persona
from amir.bot.commands import TEST_MEMBER_ID
from amir.config.settings import VoiceSettings
from amir.publishing.cards import Card
from amir.publishing.gate import ChannelKey, ChannelUnavailable, SendResult
from amir.storage.db import Database
from amir.storage.repos import VoiceHold, VoiceHoldRepo
from amir.voice.announcer import VoiceAnnouncer
from amir.voice.classify import VoiceEventKind
from amir.voice.companion import (
    AUDIT_REFRESH_DELAY_SECONDS,
    HOLD_SAVE_RETRY_FIRST_SECONDS,
    HOLD_SAVE_RETRY_MAX_SECONDS,
    KICK_CHECK_DELAY_SECONDS,
    Pin,
    VoiceCompanion,
    VoiceEvent,
)
from amir.voice.discord_adapter import AUDIT_LOG_UNREADABLE, DiscordKickDetector
from amir.voice.follow import ChannelView, VoiceCensus
from amir.voice.greetings import GreetingLibrary
from amir.voice.queue import GreetingJob, GreetingQueue, OfferResult
from amir.voice.rejoin import RejoinTracker
from amir.voice.session import PlaybackError, VoiceSession
from amir.voice.stay import StayTracker

MEMBER_ID = 123
OTHER_ID = 789
NEWCOMER_ID = 4567
GROUP = (61001, 61002, 61003, 61004, 61005)  # five friends
CHANNEL_ID = 456  # Warframe, the home channel
OTHER_CHANNEL_ID = 457  # Arcade
WF, PRED = CHANNEL_ID, OTHER_CHANNEL_ID
DBD = 458  # Dead By Daylight
TRADING = 459  # capped at 3, so never a candidate; a drag may still pin him there
AFK = 460  # the server's AFK channel
STAGE = 461  # a stage channel: never a candidate, but a drag may still pin him there
SERVER_ORDER = (WF, PRED, DBD, TRADING, AFK, STAGE)
MEMBER = f"<@{MEMBER_ID}>"
CHANNEL = f"<#{CHANNEL_ID}>"
OTHER_CHANNEL = f"<#{OTHER_CHANNEL_ID}>"
SETTLE_SECONDS = 30.0  # the default follow_settle_seconds


class Clock:
    """Monotonic clock shared by the queue, the stays and the companion, advanced by hand."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


def follow_tasks() -> "set[asyncio.Task[Any]]":
    return {t for t in asyncio.all_tasks() if t.get_name() == "voice-follow" and not t.done()}


async def followed() -> None:
    """Wait until the companion's voice-follow task has made every decision it has to make."""
    await settle()
    tasks = follow_tasks()
    if tasks:
        _, pending = await asyncio.wait(tasks, timeout=1.0)
        assert not pending, "voice-follow is still deciding"
    await settle()


class Census:
    """The companion's census: who is in each voice channel now, in the server's channel order.

    Tests move people as Discord does, before the event reaches the companion. TRADING (capped),
    AFK and STAGE are never candidates; a channel left out of `speaks` is one where Amir's role
    can't speak, so it is no candidate either. A channel deleted from `people` has vanished.
    """

    def __init__(self) -> None:
        self.people: dict[int, set[int]] = {channel: set() for channel in SERVER_ORDER}
        self.speaks: set[int] = set(SERVER_ORDER)
        self.error: Exception | None = None  # raised by every read while set
        self.empty_reads = 0  # this many reads find no guild: the census reader's EMPTY

    def __call__(self) -> VoiceCensus:
        if self.error is not None:
            raise self.error
        if self.empty_reads:
            self.empty_reads -= 1
            return VoiceCensus.EMPTY
        views = tuple(
            ChannelView(
                channel,
                frozenset(members),
                candidate=channel not in (TRADING, AFK, STAGE) and channel in self.speaks,
                can_speak=channel in self.speaks,
            )
            for channel, members in self.people.items()
        )
        return VoiceCensus(views, afk_channel_id=AFK)

    def put(self, channel_id: int, *member_ids: int) -> None:
        for member_id in member_ids:
            self.take(member_id)
            self.people[channel_id].add(member_id)

    def take(self, member_id: int) -> int | None:
        """Take `member_id` out of voice: the channel they were in, or None."""
        for channel, members in self.people.items():
            if member_id in members:
                members.discard(member_id)
                return channel
        return None


class FakeSession:
    """Stands in for VoiceSession: Amir is in `channel_id` (None: out of voice). Records connects
    and leaves in the shared order log. Each connect runs `own_connect` with the new channel
    before `channel_id` is set, as the bot's voice-snapshot task does: discord.py reports his
    arrival right after his join request, before the voice handshake is done."""

    def __init__(self, order: list[str], *, connects: bool) -> None:
        self.order = order
        self.connects = connects
        self.refuse: set[int] = set()  # channels whose every connect fails (full, say)
        self.expecting_disconnect = False
        self.channel_id: int | None = None
        self.gate: asyncio.Event | None = None  # set to hold every connect open
        self.cancelled = False  # True: every connect is cancelled, as at shutdown
        self.ensure_calls = 0
        self.targets: list[int] = []  # the channel of each ensure_connected
        self.own_connect: Callable[[int], Awaitable[None]] | None = None
        self.disconnects = 0
        self.during_disconnect: Callable[[], Awaitable[None]] | None = None  # runs in each leave
        self.presence_calls: list[str] = []
        self.returned = asyncio.Event()

    async def disconnect(self) -> None:
        self.disconnects += 1
        self.order.append("disconnect")
        if self.during_disconnect is not None:
            await self.during_disconnect()
        self.channel_id = None

    async def ensure_connected(self, channel_id: int) -> bool:
        self.ensure_calls += 1
        self.targets.append(channel_id)
        self.order.append("connect")
        if self.cancelled:
            raise asyncio.CancelledError
        if self.gate is not None:
            await self.gate.wait()
        else:
            await asyncio.sleep(0)
        self.returned.set()
        if not self.connects or channel_id in self.refuse:
            return False
        if self.own_connect is not None:
            await self.own_connect(channel_id)
        self.channel_id = channel_id
        return True

    def schedule_disconnect(self) -> None:
        self.presence_calls.append("schedule_disconnect")

    def cancel_disconnect(self) -> None:
        self.presence_calls.append("cancel_disconnect")


class RecordingQueue(GreetingQueue):
    """The real greeting queue, recording every job offered to it."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.offered: list[GreetingJob] = []

    def offer(self, job: GreetingJob) -> OfferResult:
        self.offered.append(job)
        return super().offer(job)


class FakePlayer:
    """Player port: raises queued PlaybackErrors in order, then plays."""

    def __init__(self, order: list[str], failures: Sequence[PlaybackError]) -> None:
        self.order = order
        self.failures = list(failures)
        self.attempts: list[Path] = []
        self.played: list[Path] = []
        self.listeners: list[int | None] = []
        self.finished = asyncio.Event()
        self.during_play: Callable[[], None] | None = None  # runs inside every play

    async def play(self, path: Path, listener_id: int | None) -> None:
        self.order.append("play")
        self.attempts.append(path)
        self.listeners.append(listener_id)
        if self.during_play is not None:
            self.during_play()
        await asyncio.sleep(0)
        if self.failures:
            raise self.failures.pop(0)
        self.played.append(path)
        self.finished.set()


class FakeWriter:
    """ChannelWriter: records each voice-log send in the order log, failing queued errors first."""

    def __init__(self, order: list[str], failures: Sequence[Exception]) -> None:
        self.order = order
        self.failures = list(failures)
        self.attempts = 0
        self.channels: list[ChannelKey] = []
        self.texts: list[str] = []
        self.sent = asyncio.Event()

    async def send(
        self,
        channel: ChannelKey,
        *,
        cards: Sequence[Card] = (),
        text: str | None = None,
        role_ping_id: int | None = None,
    ) -> SendResult:
        self.order.append("announce")
        self.attempts += 1
        self.channels.append(channel)
        await asyncio.sleep(0)
        if self.failures:
            raise self.failures.pop(0)
        assert text is not None
        self.texts.append(text)
        self.sent.set()
        return SendResult("sent", self.attempts)

    async def edit(
        self, channel: ChannelKey, message_id: int, *, cards: Sequence[Card]
    ) -> SendResult:
        raise AssertionError("the voice log never edits")

    async def find_recent(
        self, channel: ChannelKey, identities: set[tuple[str, str, str]], limit: int = 50
    ) -> int | None:
        raise AssertionError("the voice log never searches history")


class FakeKickDetector:
    """KickDetector port: answers whether the last disconnect was a moderator's kick."""

    def __init__(self, order: list[str]) -> None:
        self.order = order
        self.kicked = False
        self.error: Exception | None = None  # raised by every was_kicked while set
        self.snapshot_error: Exception | None = None  # raised by every snapshot while set
        self.snapshots = 0
        self.asked = 0
        self.during_snapshot: Callable[[], None] | None = None  # runs inside every snapshot
        self.gate: asyncio.Event | None = None  # set: every snapshot waits for Discord's answer

    async def snapshot(self, keep: Callable[[], bool] | None = None) -> None:
        self.snapshots += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.during_snapshot is not None:
            self.during_snapshot()
        if self.snapshot_error is not None:
            raise self.snapshot_error

    async def was_kicked(self) -> bool:
        self.asked += 1
        self.order.append("kick check")
        if self.error is not None:
            raise self.error
        return self.kicked


async def no_sleep(delay: float) -> None:
    await asyncio.sleep(0)


class SleepRecorder:
    """The companion's sleep: records each delay in the order log and moves the monotonic clock
    on by it. With `gate` set, every sleep is held until the gate opens."""

    def __init__(self, order: list[str], clock: Clock) -> None:
        self.order = order
        self.clock = clock
        self.delays: list[float] = []
        self.gate: asyncio.Event | None = None
        self.free: set[float] = set()  # delays that never wait at the gate
        self.waiting = asyncio.Event()  # set once a sleep is held at the gate

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        self.order.append("sleep")
        self.clock.advance(delay)
        if self.gate is not None and delay not in self.free:
            self.waiting.set()
            await self.gate.wait()
        else:
            await asyncio.sleep(0)


class ManualSleep:
    """A sleep that waits on the manual clock: time passes only when the test calls advance().
    (SleepRecorder moves the clock on as soon as a sleep starts, so it can't show a wait that
    ends early.)"""

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.delays: list[float] = []
        self._sleeping: list[tuple[float, asyncio.Future[None]]] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        woken = asyncio.get_running_loop().create_future()
        self._sleeping.append((self.clock() + delay, woken))
        self._wake()
        await woken

    async def advance(self, seconds: float) -> None:
        self.clock.advance(seconds)
        self._wake()
        await settle()

    def _wake(self) -> None:
        now = self.clock()
        due = [(at, woken) for at, woken in self._sleeping if at <= now or woken.done()]
        for at, woken in due:
            self._sleeping.remove((at, woken))
            if not woken.done():
                woken.set_result(None)


class VoiceChannelConnector:
    """The VoiceConnector port, under a real VoiceSession: Amir is in `channel_id` (None: out of
    voice). With `leave_gate` set, a leave waits for it, as discord.py waits for Discord to
    confirm that he left.

    A join is in his channel only once the voice handshake is done; with `handshake` set, that
    waits for it. With `report` set, each join is reported as discord.py does it: right after
    the join request, before the handshake, in a task of its own (the bot's voice-snapshot)."""

    def __init__(self, channel_id: int | None) -> None:
        self.channel_id = channel_id
        self.joined: list[int] = []
        self.leave_gate: asyncio.Event | None = None
        self.leaving = asyncio.Event()  # set once a leave waits at the gate
        self.leave_error: Exception | None = None  # raised by every leave while set
        self.handshake: asyncio.Event | None = None
        self.report: Callable[[int], Awaitable[None]] | None = None
        self.reports: list[asyncio.Task[None]] = []  # one per join reported

    async def connect(
        self,
        channel_id: int,
        timeout: float,  # noqa: ASYNC109 - protocol signature
    ) -> None:
        self.joined.append(channel_id)
        if self.report is not None:
            self.reports.append(asyncio.create_task(self.report(channel_id)))
        if self.handshake is not None:
            await self.handshake.wait()
        self.channel_id = channel_id

    async def disconnect(self) -> bool:
        if self.leave_gate is not None:
            self.leaving.set()
            await self.leave_gate.wait()
        if self.leave_error is not None:
            raise self.leave_error
        was, self.channel_id = self.channel_id, None
        return was is not None

    def is_connected(self) -> bool:
        return self.channel_id is not None

    def current_channel_id(self) -> int | None:
        return self.channel_id


@dataclasses.dataclass
class Harness:
    clock: Clock
    order: list[str]
    census: Census
    session: Any  # a FakeSession unless the test passes a real VoiceSession
    player: FakePlayer
    writer: FakeWriter
    queue: RecordingQueue
    library: GreetingLibrary
    companion: VoiceCompanion
    announcer: VoiceAnnouncer
    kicks: Any  # a FakeKickDetector unless the test passes a real detector
    sleep: Any  # a SleepRecorder, or a ManualSleep with manual_time

    def job(self, member_id: int = MEMBER_ID) -> GreetingJob:
        return GreetingJob(member_id, self.clock())

    def greeted(self) -> list[int]:
        """Everyone a greeting was offered for, in order."""
        return [job.member_id for job in self.queue.offered]

    async def arrive(self, member_id: int, channel_id: int) -> None:
        """`member_id` joins voice in `channel_id`."""
        self.census.put(channel_id, member_id)
        await self.companion.on_event(VoiceEvent(VoiceEventKind.JOIN, member_id, channel_id))

    async def hop(self, member_id: int, channel_id: int) -> None:
        """`member_id` moves from the channel they are in to `channel_id`."""
        origin = self.census.take(member_id)
        assert origin is not None
        self.census.put(channel_id, member_id)
        await self.companion.on_event(
            VoiceEvent(VoiceEventKind.MOVE, member_id, channel_id, origin)
        )

    async def depart(self, member_id: int) -> None:
        """`member_id` leaves voice."""
        origin = self.census.take(member_id)
        assert origin is not None
        await self.companion.on_event(VoiceEvent(VoiceEventKind.LEAVE, member_id, origin))

    @contextlib.asynccontextmanager
    async def running(self) -> AsyncIterator[None]:
        """Run the companion's two supervised workers: the voice log and the greeting queue."""
        tasks = [
            asyncio.create_task(self.announcer.run()),
            asyncio.create_task(self.queue.run(self.companion.play_one)),
        ]
        await settle()  # both workers are parked before any event arrives
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task


def build_harness(
    library: GreetingLibrary,
    *,
    holds: VoiceHoldRepo,
    wall_clock: Any,
    kicks: Any = None,
    session: Any = None,
    connects: bool = True,
    manual_time: bool = False,
    player_failures: Sequence[PlaybackError] = (),
    writer_failures: Sequence[Exception] = (),
    **voice: object,
) -> Harness:
    order: list[str] = []
    settings = VoiceSettings.model_validate(voice)
    census = Census()
    session = FakeSession(order, connects=connects) if session is None else session
    player = FakePlayer(order, player_failures)
    writer = FakeWriter(order, writer_failures)
    kicks = FakeKickDetector(order) if kicks is None else kicks
    clock = Clock()
    sleep = ManualSleep(clock) if manual_time else SleepRecorder(order, clock)
    queue = RecordingQueue(
        capacity=settings.queue_capacity,
        expiry_seconds=settings.greeting_expiry_seconds,
        clock=clock,
    )
    announcer = VoiceAnnouncer(writer, sleep=no_sleep)
    stays = StayTracker(
        settings.greeting_expiry_seconds,
        RejoinTracker(settings.rejoin_window_seconds, clock=clock),
        greet_on_rejoin=settings.greet_on_rejoin,
        clock=clock,
    )
    companion = VoiceCompanion(
        home_channel_id=CHANNEL_ID,
        session=session,
        queue=queue,
        library=library,
        player=player,
        announcer=announcer,
        stays=stays,
        settings=settings,
        census=census,
        settle_seconds=settings.follow_settle_seconds,
        kicks=kicks,
        holds=holds,
        wall_clock=wall_clock,
        clock=clock,
        sleep=sleep,
    )
    if isinstance(session, FakeSession):
        session.own_connect = companion.on_own_connect  # the bot's voice-snapshot, after a connect
    return Harness(
        clock,
        order,
        census,
        session,
        player,
        writer,
        queue,
        library,
        companion,
        announcer,
        kicks,
        sleep,
    )


MakeHarness = Callable[..., Harness]


@pytest.fixture
def holds(db: Database) -> VoiceHoldRepo:
    """The real hold record, in a real database."""
    return VoiceHoldRepo(db)


@pytest.fixture
def make_harness(holds: VoiceHoldRepo, clock) -> MakeHarness:
    """build_harness over the test's database: every harness of a test shares its holds, and
    holds and pins are stamped by the shared wall clock."""
    return functools.partial(build_harness, holds=holds, wall_clock=clock)


def copy_clips(directory: Path, clip: Path, count: int) -> None:
    directory.mkdir()
    for n in range(count):
        shutil.copyfile(clip, directory / f"{clip.stem}-{n}{clip.suffix}")


async def scan_copies(directory: Path, clip: Path, ffmpeg: str, count: int) -> GreetingLibrary:
    """A real library over `count` copies of the test clip (zero gives an empty library)."""
    copy_clips(directory, clip, count)
    library = GreetingLibrary(directory, ffmpeg)
    report = await library.scan()
    assert len(report.valid) == count
    return library


@pytest.fixture
async def one_clip(tmp_path: Path, voice_clip: Path, ffmpeg_exe: str) -> GreetingLibrary:
    return await scan_copies(tmp_path / "one", voice_clip, ffmpeg_exe, 1)


@pytest.fixture
async def two_clips(tmp_path: Path, voice_clip: Path, ffmpeg_exe: str) -> GreetingLibrary:
    return await scan_copies(tmp_path / "two", voice_clip, ffmpeg_exe, 2)


@pytest.fixture
async def no_clips(tmp_path: Path, voice_clip: Path, ffmpeg_exe: str) -> GreetingLibrary:
    return await scan_copies(tmp_path / "none", voice_clip, ffmpeg_exe, 0)


def amir_with(h: Harness, channel_id: int, *member_ids: int) -> None:
    """Amir is in `channel_id`, with `member_ids` (people in voice since before he started:
    nobody is owed a greeting)."""
    h.census.put(channel_id, *member_ids)
    h.session.channel_id = channel_id


# --- persona --------------------------------------------------------------------------


def test_voice_lines_cannot_ping():
    for line in (
        persona.voice_join_line("<@123>", "<#456>"),
        persona.voice_leave_line("<@123>", "<#456>"),
    ):
        assert "<@123>" in line
        assert "<#456>" in line
        assert "@everyone" not in line.lower()
        assert "@here" not in line.lower()
    assert persona.voice_join_line("<@123>", "<#456>") != persona.voice_leave_line(
        "<@123>", "<#456>"
    )


def test_the_move_line_names_both_channels_and_cannot_ping():
    line = persona.voice_move_line("<@123>", "<#456>", "<#457>")
    assert line == "<@123> has moved from <#456> to <#457>."
    assert "@everyone" not in line.lower()
    assert "@here" not in line.lower()


# --- on_event: lines ------------------------------------------------------------------


async def test_join_announces_before_greeting_and_connects(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    h = make_harness(one_clip)
    async with h.running():
        await h.arrive(MEMBER_ID, WF)
        await asyncio.wait_for(h.player.finished.wait(), 1.0)
    assert h.order == ["announce", "connect", "play"]
    assert h.writer.texts == [persona.voice_join_line(MEMBER, CHANNEL)]
    assert h.writer.channels == [ChannelKey.VOICE_LOG]
    assert len(h.player.played) == 1
    assert h.player.listeners == [MEMBER_ID]  # it waits until the arrival can hear it


async def test_announcement_survives_voice_failure(
    make_harness: MakeHarness, one_clip: GreetingLibrary, caplog: pytest.LogCaptureFixture
):
    h = make_harness(one_clip, connects=False)
    with caplog.at_level(logging.DEBUG):
        async with h.running():
            await h.arrive(MEMBER_ID, WF)
            await asyncio.wait_for(h.writer.sent.wait(), 1.0)
            await followed()
    assert h.writer.texts == [persona.voice_join_line(MEMBER, CHANNEL)]
    assert h.session.ensure_calls == 1
    assert h.player.attempts == []  # nobody is greeted into nothing
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]  # nothing raised


async def test_announcement_does_not_wait_for_a_hanging_connect(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    h = make_harness(one_clip)
    h.session.gate = asyncio.Event()  # the connect never completes
    async with h.running():
        await asyncio.wait_for(h.arrive(MEMBER_ID, WF), 1.0)
        await asyncio.wait_for(h.writer.sent.wait(), 1.0)
        assert h.session.ensure_calls == 1  # the follow task is still connecting
    assert h.writer.texts == [persona.voice_join_line(MEMBER, CHANNEL)]
    assert h.player.attempts == []


async def test_announce_flags_respected(make_harness: MakeHarness, one_clip: GreetingLibrary):
    quiet_joins = make_harness(one_clip, announce_joins=False)
    async with quiet_joins.running():
        await quiet_joins.arrive(MEMBER_ID, WF)
        await asyncio.wait_for(quiet_joins.player.finished.wait(), 1.0)
        await settle()
    assert quiet_joins.writer.attempts == 0  # no line ...
    assert len(quiet_joins.player.played) == 1  # ... but still a greeting

    quiet_leaves = make_harness(one_clip, announce_leaves=False)
    amir_with(quiet_leaves, WF, MEMBER_ID)
    async with quiet_leaves.running():
        await quiet_leaves.depart(MEMBER_ID)
        await followed()
    assert quiet_leaves.writer.attempts == 0
    assert quiet_leaves.session.presence_calls == ["schedule_disconnect"]


async def test_an_arrival_elsewhere_is_logged_but_not_greeted(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    h = make_harness(one_clip)
    amir_with(h, WF, MEMBER_ID, OTHER_ID)
    async with h.running():
        await h.arrive(NEWCOMER_ID, PRED)
        await asyncio.wait_for(h.writer.sent.wait(), 1.0)
        await followed()
    assert h.writer.texts == [persona.voice_join_line(f"<@{NEWCOMER_ID}>", OTHER_CHANNEL)]
    assert h.greeted() == []
    assert h.session.ensure_calls == 0  # his own channel has more people
    assert h.player.attempts == []


async def test_a_departure_elsewhere_is_logged_without_touching_presence(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    h = make_harness(one_clip)
    h.census.put(TRADING, MEMBER_ID)  # Amir is out of voice, and can't sit in Trading
    async with h.running():
        await h.depart(MEMBER_ID)
        await asyncio.wait_for(h.writer.sent.wait(), 1.0)
        await followed()
    assert h.writer.texts == [persona.voice_leave_line(MEMBER, f"<#{TRADING}>")]
    assert h.session.presence_calls == []
    assert h.session.ensure_calls == 0


async def test_a_move_is_one_line(make_harness: MakeHarness, one_clip: GreetingLibrary):
    h = make_harness(one_clip)
    amir_with(h, WF, MEMBER_ID, OTHER_ID)
    async with h.running():
        await h.hop(MEMBER_ID, PRED)
        await asyncio.wait_for(h.writer.sent.wait(), 1.0)
        await followed()
    assert h.writer.texts == [persona.voice_move_line(MEMBER, CHANNEL, OTHER_CHANNEL)]


async def test_moves_can_be_switched_off(make_harness: MakeHarness, one_clip: GreetingLibrary):
    h = make_harness(one_clip, announce_moves=False)
    amir_with(h, WF, MEMBER_ID)
    await h.hop(MEMBER_ID, AFK)
    await followed()
    assert h.writer.attempts == 0
    # Presence is not a log matter: someone parked in AFK isn't really there.
    assert h.session.presence_calls == ["schedule_disconnect"]


# --- presence: Amir sits with the biggest group ------------------------------------------


async def test_a_first_arrival_brings_him_and_is_greeted(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    h = make_harness(one_clip)
    async with h.running():
        await h.arrive(MEMBER_ID, PRED)
        await asyncio.wait_for(h.player.finished.wait(), 1.0)
    assert h.session.targets == [PRED]  # at once: out of voice he leaves nobody behind
    assert h.sleep.delays == []
    assert h.kicks.snapshots == 1  # his arrival notes the audit log
    assert h.greeted() == [MEMBER_ID]
    assert h.player.listeners == [MEMBER_ID]


@pytest.mark.parametrize("first", ["audit log", "handshake"])
async def test_the_first_arrival_is_greeted_whichever_ends_first_his_handshake_or_audit_read(
    make_harness: MakeHarness, one_clip: GreetingLibrary, first: str
):
    # A race: discord.py reports Amir's own join right after his join request,
    # before the voice handshake is done, and the audit log read that starts can answer first.
    connector = VoiceChannelConnector(None)
    session = VoiceSession(connector, grace_seconds=60, connect_timeout=15.0, max_attempts=1)
    h = make_harness(one_clip, session=session)
    connector.report = h.companion.on_own_connect  # the bot's voice-snapshot
    connector.handshake = asyncio.Event()
    if first == "handshake":
        h.kicks.gate = asyncio.Event()
    joined = h.clock()
    await h.arrive(MEMBER_ID, PRED)  # Amir is out of voice: he goes to them at once
    await settle()
    assert connector.joined == [PRED]
    assert session.channel_id is None  # his join is reported, the handshake still runs
    if first == "audit log":
        await asyncio.wait_for(asyncio.gather(*connector.reports), 1.0)
        connector.handshake.set()
        await followed()
    else:
        connector.handshake.set()
        await followed()
        h.kicks.gate.set()
        await asyncio.wait_for(asyncio.gather(*connector.reports), 1.0)
        await followed()
    assert session.channel_id == PRED
    assert h.kicks.snapshots == 1  # his arrival notes the audit log
    assert h.queue.offered == [GreetingJob(MEMBER_ID, joined)]  # once, from their join


async def test_the_group_drifting_moves_him_after_the_settle_time(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    h = make_harness(one_clip)
    amir_with(h, WF, MEMBER_ID, OTHER_ID)
    h.census.put(PRED, *GROUP[:2])
    h.sleep.gate = asyncio.Event()
    await h.hop(MEMBER_ID, PRED)  # Arcade 3, Warframe 1
    await asyncio.wait_for(h.sleep.waiting.wait(), 1.0)
    assert h.sleep.delays == [SETTLE_SECONDS]
    assert h.session.ensure_calls == 0  # no move before 30 s
    h.sleep.gate.set()  # the settle time is up, and Arcade still has more people
    await followed()
    assert h.session.targets == [PRED]


async def test_a_peek_never_moves_him(make_harness: MakeHarness, one_clip: GreetingLibrary):
    h = make_harness(one_clip)
    amir_with(h, WF, MEMBER_ID, OTHER_ID)
    h.census.put(PRED, *GROUP[:2])
    h.sleep.gate = asyncio.Event()
    await h.hop(MEMBER_ID, PRED)  # Arcade goes ahead...
    await asyncio.wait_for(h.sleep.waiting.wait(), 1.0)
    await h.hop(MEMBER_ID, WF)  # ...and is back to fewer within 30 s
    await followed()  # the wait ended with the change: nothing waits out the settle time
    h.sleep.gate.set()
    await followed()
    assert h.session.ensure_calls == 0
    assert h.sleep.delays == [SETTLE_SECONDS]


async def test_a_change_mid_settle_waits_only_the_remainder(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    h = make_harness(one_clip, manual_time=True)
    amir_with(h, WF, MEMBER_ID, OTHER_ID)
    h.census.put(PRED, *GROUP[:2])
    await h.hop(MEMBER_ID, PRED)  # Arcade 3, Warframe 1: the settle time begins
    await settle()
    assert h.sleep.delays == [SETTLE_SECONDS]
    await h.sleep.advance(10)
    await h.arrive(NEWCOMER_ID, PRED)  # 10 s in, Arcade grows: the same group still wins
    await settle()
    assert h.sleep.delays == [SETTLE_SECONDS, 20.0]  # the rest of the 30 s, not a fresh 30
    await h.sleep.advance(19)
    assert h.session.ensure_calls == 0  # no early move
    await h.sleep.advance(1)
    await followed()
    assert h.session.targets == [PRED]


async def test_changes_every_few_seconds_never_restart_the_settle_time(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    h = make_harness(one_clip, manual_time=True)
    amir_with(h, WF, MEMBER_ID, OTHER_ID)
    h.census.put(PRED, *GROUP[:2])
    await h.hop(MEMBER_ID, PRED)  # Arcade goes ahead and stays ahead
    await settle()
    for _ in range(5):  # a busy server: someone comes or goes elsewhere every 5 s
        await h.sleep.advance(5)
        if NEWCOMER_ID in h.census.people[DBD]:
            await h.depart(NEWCOMER_ID)
        else:
            await h.arrive(NEWCOMER_ID, DBD)
        await settle()
    assert h.session.ensure_calls == 0  # 25 s in
    assert h.sleep.delays == [SETTLE_SECONDS, 25.0, 20.0, 15.0, 10.0, 5.0]
    await h.sleep.advance(5)
    await followed()
    assert h.session.targets == [PRED]  # 30 s after Arcade first had the most people


async def test_his_empty_channel_sends_him_to_the_group_at_once(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    h = make_harness(one_clip)
    amir_with(h, WF, MEMBER_ID)
    h.census.put(PRED, OTHER_ID, NEWCOMER_ID)
    await h.depart(MEMBER_ID)  # Warframe empties
    await followed()
    assert h.session.targets == [PRED]
    assert h.sleep.delays == []  # nobody is left behind: no settle time


async def test_nobody_left_starts_the_grace_leave(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    h = make_harness(one_clip)
    amir_with(h, WF, MEMBER_ID)
    h.census.put(AFK, OTHER_ID)  # parked in AFK: not really there
    h.census.put(TRADING, NEWCOMER_ID)  # capped: he can't sit with them
    await h.depart(MEMBER_ID)
    await followed()
    assert h.session.presence_calls == ["schedule_disconnect"]
    assert h.session.ensure_calls == 0


async def test_an_arrival_elsewhere_during_the_grace_leave_brings_him_at_once(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    h = make_harness(one_clip)
    amir_with(h, WF, MEMBER_ID)
    async with h.running():
        await h.depart(MEMBER_ID)
        await followed()
        assert h.session.presence_calls == ["schedule_disconnect"]
        await h.arrive(NEWCOMER_ID, PRED)
        await asyncio.wait_for(h.player.finished.wait(), 1.0)
    assert h.session.targets == [PRED]
    assert h.sleep.delays == []
    assert h.greeted() == [NEWCOMER_ID]
    assert h.player.listeners == [NEWCOMER_ID]


async def test_an_arrival_as_his_grace_leave_runs_brings_him_back_and_is_greeted(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    connector = VoiceChannelConnector(WF)
    session = VoiceSession(connector, grace_seconds=0, connect_timeout=15.0, max_attempts=1)
    h = make_harness(one_clip, session=session)
    h.census.put(WF, OTHER_ID)
    connector.leave_gate = asyncio.Event()  # Discord takes a moment to confirm his leave
    await h.depart(OTHER_ID)  # nobody is left: the grace leave starts...
    await asyncio.wait_for(connector.leaving.wait(), 1.0)  # ...its time is up, and he is leaving
    assert session.expecting_disconnect
    joined = h.clock()
    await h.arrive(MEMBER_ID, WF)  # a member joins Warframe just as he leaves it
    await settle()
    assert h.greeted() == []  # not while he is on his way out
    connector.leave_gate.set()
    await followed()
    assert connector.joined == [WF]  # he comes back to them...
    assert h.queue.offered == [GreetingJob(MEMBER_ID, joined)]  # ...and greets them once
    await h.companion.on_own_connect(WF)  # Discord reports his arrival: no second greeting
    await followed()
    assert h.queue.offered == [GreetingJob(MEMBER_ID, joined)]
    assert connector.joined == [WF]


async def test_an_arrival_as_a_refused_grace_leave_runs_is_greeted(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    connector = VoiceChannelConnector(WF)
    session = VoiceSession(connector, grace_seconds=0, connect_timeout=15.0, max_attempts=1)
    h = make_harness(one_clip, session=session)
    h.census.put(WF, OTHER_ID)
    connector.leave_gate = asyncio.Event()
    connector.leave_error = RuntimeError("voice websocket already closed")  # Discord refuses it
    await h.depart(OTHER_ID)  # nobody is left: the grace leave starts, and its time is up
    await asyncio.wait_for(connector.leaving.wait(), 1.0)
    joined = h.clock()
    await h.arrive(MEMBER_ID, WF)  # a member joins Warframe just as he leaves it
    await settle()
    assert h.greeted() == []  # not while he is on his way out
    connector.leave_gate.set()  # ...and then he never left
    await followed()
    assert session.channel_id == WF
    assert connector.joined == []  # still there: nothing to rejoin
    assert h.queue.offered == [GreetingJob(MEMBER_ID, joined)]  # greeted all the same


async def test_last_leave_schedules_disconnect_and_join_cancels(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    h = make_harness(one_clip)
    amir_with(h, WF, MEMBER_ID, OTHER_ID)
    await h.depart(OTHER_ID)
    await followed()
    assert "schedule_disconnect" not in h.session.presence_calls  # someone is still there
    await h.depart(MEMBER_ID)
    await followed()
    assert h.session.presence_calls[-1] == "schedule_disconnect"
    await h.arrive(NEWCOMER_ID, WF)
    await followed()
    assert h.session.presence_calls[-1] == "cancel_disconnect"  # he stays: someone came
    assert h.session.ensure_calls == 0
    assert h.greeted() == [NEWCOMER_ID]


async def test_a_move_out_of_his_channel_leaves_him_while_others_stay(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    h = make_harness(one_clip)
    amir_with(h, WF, MEMBER_ID, OTHER_ID)
    await h.hop(MEMBER_ID, PRED)  # a tie: he stays where he is
    await followed()
    assert "schedule_disconnect" not in h.session.presence_calls
    assert h.session.ensure_calls == 0
    assert h.sleep.delays == []


async def test_a_move_between_other_channels_is_only_logged(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    h = make_harness(one_clip)
    amir_with(h, WF, MEMBER_ID, OTHER_ID)
    h.census.put(DBD, NEWCOMER_ID)
    async with h.running():
        await h.hop(NEWCOMER_ID, PRED)
        await asyncio.wait_for(h.writer.sent.wait(), 1.0)
        await followed()
    assert "schedule_disconnect" not in h.session.presence_calls
    assert h.session.ensure_calls == 0
    assert h.greeted() == []


async def test_a_follow_that_finds_nobody_does_not_connect(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    h = make_harness(one_clip)
    h.census.put(WF, MEMBER_ID)
    await h.depart(MEMBER_ID)
    await h.arrive(MEMBER_ID, WF)  # a follow is due...
    await h.depart(MEMBER_ID)  # ...but they are gone again before it runs
    await followed()
    assert h.session.ensure_calls == 0  # connecting now would only leave again
    assert h.session.presence_calls == []  # out of voice: nothing to leave


async def test_voice_changes_share_one_follow_task(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    h = make_harness(one_clip)
    h.session.gate = asyncio.Event()  # connecting is slow (or failing)
    await h.arrive(MEMBER_ID, WF)
    await settle()
    await h.arrive(OTHER_ID, WF)
    await h.arrive(NEWCOMER_ID, PRED)
    await settle()
    assert len(follow_tasks()) == 1
    assert h.session.ensure_calls == 1  # one connect, not one per change
    h.session.gate.set()
    await followed()
    assert h.session.targets == [WF]  # then one more decision: he stays with the most people


# --- pins and AFK ---------------------------------------------------------------------


async def test_a_drag_pins_him_against_a_bigger_group(
    make_harness: MakeHarness, one_clip: GreetingLibrary, holds: VoiceHoldRepo, clock
):
    h = make_harness(one_clip)
    amir_with(h, PRED, *GROUP[:3])
    await h.arrive(MEMBER_ID, TRADING)  # joins voice in Trading: owed a greeting this stay
    await followed()
    h.session.channel_id = TRADING  # a moderator drags Amir in with them
    await h.companion.on_own_move(TRADING)
    await followed()
    await h.arrive(NEWCOMER_ID, PRED)  # Arcade grows, and still he stays
    await followed()
    assert h.session.ensure_calls == 0
    assert h.sleep.delays == []
    assert h.companion.pin == Pin(TRADING, clock.now())
    assert h.greeted() == []  # nobody is greeted for a drag
    assert h.companion.hold is None
    assert await holds.get() is None
    assert h.kicks.asked == 0


async def test_a_drag_into_an_empty_channel_pins_nothing(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    h = make_harness(one_clip)
    h.census.put(WF, MEMBER_ID, OTHER_ID)
    h.session.channel_id = DBD  # dragged into Dead By Daylight, where nobody is
    await h.companion.on_own_move(DBD)
    await followed()
    assert h.companion.pin is None
    assert h.session.targets == [WF]  # straight back to the group
    assert h.sleep.delays == []


async def test_the_pin_ends_when_its_channel_empties(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    h = make_harness(one_clip)
    amir_with(h, TRADING, MEMBER_ID)
    h.census.put(PRED, *GROUP[:3])
    await h.companion.on_own_move(TRADING)
    await followed()
    assert h.companion.pin is not None
    await h.depart(MEMBER_ID)
    await followed()
    assert h.companion.pin is None
    assert h.session.targets == [PRED]  # then he follows the crowd, at once: nobody is left
    assert h.sleep.delays == []


async def test_a_pin_on_a_vanished_channel_ends(
    make_harness: MakeHarness, one_clip: GreetingLibrary, holds: VoiceHoldRepo
):
    h = make_harness(one_clip)
    amir_with(h, TRADING, MEMBER_ID)
    h.census.put(PRED, *GROUP[:3])
    await h.companion.on_own_move(TRADING)
    await followed()
    del h.census.people[TRADING]  # the channel is deleted, which drops everyone in it
    h.session.channel_id = None
    await h.companion.on_own_disconnect()  # not a kick: no moderator disconnected him
    await followed()
    assert h.companion.pin is None
    assert h.session.targets == [PRED]
    assert h.companion.hold is None
    assert await holds.get() is None


async def test_a_drag_into_a_stage_channel_pins_him(
    make_harness: MakeHarness, one_clip: GreetingLibrary, clock
):
    h = make_harness(one_clip)
    amir_with(h, STAGE, MEMBER_ID)  # a moderator drags him onto the stage, with someone there
    h.census.put(PRED, *GROUP[:3])
    await h.companion.on_own_move(STAGE)
    await followed()
    assert h.companion.pin == Pin(STAGE, clock.now())
    assert h.session.ensure_calls == 0


async def test_an_unreadable_census_never_ends_a_pin(
    make_harness: MakeHarness, one_clip: GreetingLibrary, clock
):
    h = make_harness(one_clip)
    amir_with(h, TRADING, MEMBER_ID)
    h.census.put(PRED, *GROUP[:3])
    await h.companion.on_own_move(TRADING)
    await followed()
    h.census.empty_reads = 1  # the guild is briefly unavailable: nobody seems to be anywhere
    await h.arrive(NEWCOMER_ID, DBD)
    await followed()
    assert h.companion.pin == Pin(TRADING, clock.now())
    await h.arrive(OTHER_ID, DBD)  # the next read is whole again
    await followed()
    assert h.companion.pin == Pin(TRADING, clock.now())
    assert h.session.ensure_calls == 0  # still with them in Trading
    assert h.session.presence_calls[-1] == "cancel_disconnect"


async def test_a_pinned_channel_that_refuses_him_ends_the_pin(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    h = make_harness(one_clip)
    amir_with(h, TRADING, MEMBER_ID)
    h.census.put(PRED, *GROUP[:3])
    await h.companion.on_own_move(TRADING)
    await followed()
    h.session.channel_id = None  # dropped by accident...
    h.session.refuse.add(TRADING)  # ...and Trading is full now: he can't get back in
    await h.companion.on_own_disconnect()
    await followed()
    assert h.companion.pin is None
    assert h.session.targets == [TRADING, PRED]  # back to the crowd, not stuck out of voice


@pytest.mark.parametrize("crowd", [True, False], ids=["a group elsewhere", "nobody"])
async def test_a_move_to_afk_is_never_a_hold_or_a_pin(
    make_harness: MakeHarness, one_clip: GreetingLibrary, holds: VoiceHoldRepo, crowd: bool
):
    h = make_harness(one_clip)
    amir_with(h, TRADING, OTHER_ID)
    await h.companion.on_own_move(TRADING)  # pinned in Trading first
    await followed()
    if crowd:
        h.census.put(PRED, *GROUP[:2])
    h.session.channel_id = AFK  # Discord's idle move, or a moderator's drag
    await h.companion.on_own_move(AFK)
    await followed()
    assert h.companion.hold is None
    assert await holds.get() is None
    assert h.companion.pin is None
    assert h.kicks.asked == 0
    if crowd:
        assert h.session.targets == [PRED]  # the busiest channel he can join, at once
    else:
        # Back with the people in Trading? No: AFK ends the pin; with nobody to follow he leaves.
        assert h.session.targets == []
        assert h.session.presence_calls[-1] == "schedule_disconnect"


async def test_no_greeting_where_he_cannot_speak(
    make_harness: MakeHarness, one_clip: GreetingLibrary, caplog: pytest.LogCaptureFixture
):
    h = make_harness(one_clip)
    h.census.speaks.discard(TRADING)  # his role may not speak in Trading
    amir_with(h, TRADING, OTHER_ID)
    await h.companion.on_own_move(TRADING)  # pinned there by a drag
    await followed()
    with caplog.at_level(logging.DEBUG, logger="amir.voice.companion"):
        async with h.running():
            await h.arrive(MEMBER_ID, TRADING)
            await followed()
            await settle()
    assert h.greeted() == [MEMBER_ID]  # offered as he arrived, and dropped by the worker
    assert h.player.attempts == []
    assert len(h.queue) == 0
    assert "greeting dropped: Amir cannot speak in his voice channel" in caplog.messages
    assert str(MEMBER_ID) not in caplog.text


# --- kicks, release, startup and shutdown ------------------------------------------------


async def test_a_kick_during_a_settle_wait_keeps_him_out(
    make_harness: MakeHarness, one_clip: GreetingLibrary, holds: VoiceHoldRepo
):
    h = make_harness(one_clip)
    amir_with(h, WF, MEMBER_ID, OTHER_ID)
    h.census.put(PRED, *GROUP[:2])
    h.kicks.kicked = True
    h.sleep.gate = asyncio.Event()
    await h.hop(MEMBER_ID, PRED)  # Arcade goes ahead: the settle wait begins
    await asyncio.wait_for(h.sleep.waiting.wait(), 1.0)
    h.session.channel_id = None  # a moderator disconnects him
    check = asyncio.create_task(h.companion.on_own_disconnect())
    await settle()  # the kick check is pending too
    h.sleep.gate.set()  # the settle time is up first, while the check is still pending
    await asyncio.wait_for(check, 1.0)
    await followed()
    hold = h.companion.hold
    assert hold is not None and hold.reason == "kicked"
    assert await holds.get() == hold
    assert h.session.ensure_calls == 0  # he stays out


async def test_release_clears_the_pin_and_follows_the_crowd(
    make_harness: MakeHarness, one_clip: GreetingLibrary, caplog: pytest.LogCaptureFixture
):
    h = make_harness(one_clip)
    amir_with(h, TRADING, MEMBER_ID)
    h.census.put(PRED, *GROUP[:3])
    await h.companion.on_own_move(TRADING)
    await followed()
    assert h.companion.pin is not None
    with caplog.at_level(logging.INFO, logger="amir.voice.companion"):
        assert await h.companion.release_hold() is False  # no hold was lifted, only the pin
    assert h.companion.pin is None
    await followed()
    assert h.session.targets == [PRED]
    assert h.sleep.delays == [SETTLE_SECONDS]  # someone is still with him: the settle time
    assert "voice hold cleared by an admin" not in caplog.messages


async def test_decided_waits_for_the_follow_decision_and_its_connect(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    h = make_harness(one_clip)
    await asyncio.wait_for(h.companion.decided(), 1.0)  # nothing to decide yet
    h.session.gate = asyncio.Event()
    await h.arrive(MEMBER_ID, PRED)
    waiting = asyncio.create_task(h.companion.decided())
    await settle()
    assert h.session.targets == [PRED]
    assert not waiting.done()  # his connect is under way
    h.session.gate.set()
    await asyncio.wait_for(waiting, 1.0)
    assert h.session.channel_id == PRED


async def test_decided_never_waits_out_a_settle_time(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    h = make_harness(one_clip)
    amir_with(h, WF, MEMBER_ID, OTHER_ID)
    h.census.put(PRED, *GROUP[:2])
    h.sleep.gate = asyncio.Event()
    await h.hop(MEMBER_ID, PRED)  # Arcade 3, Warframe 1: the settle time begins
    await asyncio.wait_for(h.sleep.waiting.wait(), 1.0)
    await asyncio.wait_for(h.companion.decided(), 1.0)  # he stays put for now: that is decided
    assert h.session.ensure_calls == 0
    h.session.gate = asyncio.Event()
    h.sleep.gate.set()  # the settle time is up and Arcade still has more: he moves
    await settle()
    waiting = asyncio.create_task(h.companion.decided())
    await settle()
    assert h.session.targets == [PRED]
    assert not waiting.done()  # the move is under way
    h.session.gate.set()
    await asyncio.wait_for(waiting, 1.0)
    assert h.session.channel_id == PRED


async def test_decided_never_waits_after_shutdown(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    h = make_harness(one_clip)
    await h.arrive(MEMBER_ID, PRED)  # voice-follow is created, but has not taken a step yet
    await h.companion.cancel_background()  # shutdown cancels it before its first step

    await asyncio.wait_for(h.companion.decided(), 1.0)

    assert h.session.ensure_calls == 0


async def test_voice_census_is_the_census_it_reads(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    h = make_harness(one_clip)
    h.census.put(PRED, MEMBER_ID, OTHER_ID)
    h.census.put(AFK, NEWCOMER_ID)

    assert h.companion.voice_census() == h.census()


async def test_startup_joins_the_busiest_channel_quietly(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    h = make_harness(one_clip)
    h.census.put(WF, MEMBER_ID)
    h.census.put(PRED, OTHER_ID, NEWCOMER_ID)
    async with h.running():
        await h.companion.on_startup()
        await followed()
    assert h.session.targets == [PRED]
    assert h.kicks.snapshots == 2  # at startup, and as he joins
    assert h.writer.attempts == 0  # no lines...
    assert h.greeted() == []  # ...and no greetings: they didn't just arrive
    assert h.player.attempts == []


async def test_startup_without_humans_stays_out(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    h = make_harness(one_clip)
    h.census.put(AFK, MEMBER_ID)
    await h.companion.on_startup()
    await followed()
    assert h.session.ensure_calls == 0
    assert h.session.presence_calls == []


@pytest.mark.parametrize("people", [False, True], ids=["nobody", "people"])
async def test_startup_reads_the_audit_log(
    make_harness: MakeHarness, one_clip: GreetingLibrary, people: bool
):
    h = make_harness(one_clip)
    h.session.own_connect = None  # his arrival reads it again: the bot's doing, not startup's
    if people:
        h.census.put(WF, MEMBER_ID, OTHER_ID)
    await h.companion.on_startup()
    await followed()
    assert h.kicks.snapshots == 1  # whether or not he joins: a later kick can be told apart
    assert h.session.ensure_calls == (1 if people else 0)


async def test_startup_joins_even_if_the_audit_log_snapshot_fails(
    make_harness: MakeHarness, one_clip: GreetingLibrary, caplog: pytest.LogCaptureFixture
):
    h = make_harness(one_clip)
    h.session.own_connect = None
    h.census.put(WF, MEMBER_ID)
    h.kicks.snapshot_error = RuntimeError("audit log unavailable")
    with caplog.at_level(logging.WARNING, logger="amir.voice.companion"):
        await h.companion.on_startup()
        await followed()
    assert h.session.ensure_calls == 1
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "RuntimeError" in warnings[0]


async def test_joining_voice_reads_the_audit_log(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    h = make_harness(one_clip)
    amir_with(h, WF, MEMBER_ID)
    await h.companion.on_own_connect(WF)
    await followed()
    assert h.kicks.snapshots == 1
    assert h.session.ensure_calls == 0
    assert h.session.presence_calls == []  # that is all it does: the follow greets and decides


async def test_an_accidental_disconnect_returns_him_to_the_crowd_quietly(
    make_harness: MakeHarness, one_clip: GreetingLibrary, holds: VoiceHoldRepo
):
    h = make_harness(one_clip)
    h.census.put(WF, MEMBER_ID)  # he was in Warframe; the group is in Arcade now
    h.census.put(PRED, OTHER_ID, NEWCOMER_ID)
    async with h.running():
        await h.companion.on_own_disconnect()  # the kick detector says: not a kick
        await followed()
    # The audit log is read only after the delay: its entry can lag the disconnect.
    assert h.order[:2] == ["sleep", "kick check"]
    assert h.sleep.delays == [KICK_CHECK_DELAY_SECONDS]
    assert h.session.targets == [PRED]  # the crowd, not home
    assert h.writer.attempts == 0
    assert h.greeted() == []
    assert h.player.attempts == []
    assert h.companion.hold is None
    assert await holds.get() is None


async def test_forced_disconnect_with_nobody_present_does_nothing(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    h = make_harness(one_clip)
    await h.companion.on_own_disconnect()
    await followed()
    assert h.session.ensure_calls == 0
    assert h.session.presence_calls == []


async def test_failed_kick_check_counts_as_an_accident(
    make_harness: MakeHarness,
    one_clip: GreetingLibrary,
    holds: VoiceHoldRepo,
    caplog: pytest.LogCaptureFixture,
):
    h = make_harness(one_clip)
    h.census.put(WF, MEMBER_ID)
    h.kicks.error = RuntimeError("audit log unavailable")
    with caplog.at_level(logging.WARNING, logger="amir.voice.companion"):
        await h.companion.on_own_disconnect()
        await followed()
    assert h.session.targets == [WF]  # he rejoins
    assert await holds.get() is None
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "RuntimeError" in warnings[0]


async def test_cancel_background_stops_the_follow_task(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    h = make_harness(one_clip)
    h.session.gate = asyncio.Event()  # the follow task's connect hangs
    await h.arrive(MEMBER_ID, WF)
    await settle()
    assert h.session.ensure_calls == 1

    await asyncio.wait_for(h.companion.cancel_background(), 1)  # shutdown

    assert not follow_tasks()
    h.session.gate.set()
    await settle()
    assert h.session.returned.is_set() is False  # the cancelled connect never completed
    await h.arrive(OTHER_ID, PRED)  # nothing starts it again
    await settle()
    assert not follow_tasks()
    assert h.session.ensure_calls == 1


async def test_cancel_background_without_a_follow_is_a_no_op(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    h = make_harness(one_clip)
    await asyncio.wait_for(h.companion.cancel_background(), 1)


# --- welcome rules: one greeting per stay in voice -----------------------------------------


async def test_a_hop_back_into_his_channel_is_not_greeted(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    h = make_harness(one_clip)
    amir_with(h, WF, OTHER_ID)
    await h.arrive(MEMBER_ID, WF)  # greeted
    await followed()
    await h.hop(MEMBER_ID, PRED)
    await followed()
    await h.hop(MEMBER_ID, WF)  # back: the same stay
    await followed()
    assert h.greeted() == [MEMBER_ID]
    assert h.session.ensure_calls == 0


async def test_moving_with_the_group_greets_nobody_twice(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    h = make_harness(one_clip)
    async with h.running():
        for member_id in GROUP:
            await h.arrive(member_id, WF)
            await followed()
        assert h.session.targets == [WF]
        for member_id in GROUP[:3]:
            await h.hop(member_id, PRED)  # the group drifts to Arcade...
            await followed()
        assert h.session.targets == [WF, PRED]  # ...and Amir follows, after the settle time
        h.clock.advance(240)
        for member_id in GROUP[3:]:
            await h.hop(member_id, PRED)  # the last two, 4 minutes later
            await followed()
        await settle()
    assert h.sleep.delays == [SETTLE_SECONDS]
    assert sorted(h.greeted()) == sorted(GROUP)  # five greetings in all
    assert len(h.player.played) == 5


async def test_someone_who_joined_elsewhere_is_greeted_when_they_come_to_him(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    h = make_harness(one_clip)
    amir_with(h, WF, OTHER_ID, NEWCOMER_ID)
    await h.arrive(MEMBER_ID, DBD)
    await followed()
    assert h.greeted() == []  # not his channel
    await h.hop(MEMBER_ID, WF)
    await followed()
    assert h.greeted() == [MEMBER_ID]
    assert h.session.ensure_calls == 0


async def test_a_newcomer_he_catches_within_thirty_seconds_is_greeted(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    h = make_harness(one_clip)
    amir_with(h, WF, OTHER_ID)
    joined = h.clock()
    await h.arrive(NEWCOMER_ID, PRED)  # a tie: he stays in Warframe
    await followed()
    assert h.session.ensure_calls == 0
    h.clock.advance(10)
    await h.depart(OTHER_ID)  # Warframe empties: he goes to Arcade at once
    await followed()
    assert h.session.targets == [PRED]
    assert h.queue.offered == [GreetingJob(NEWCOMER_ID, joined)]  # the 30 s count from joining


async def test_people_there_longer_are_not_greeted_when_he_moves_in(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    h = make_harness(one_clip)
    amir_with(h, WF, OTHER_ID)
    await h.arrive(NEWCOMER_ID, PRED)  # in Arcade from now on: a tie, so he stays
    await followed()
    h.clock.advance(60)
    await h.depart(OTHER_ID)  # a minute later Warframe empties: he goes to them at once
    await followed()
    assert h.session.targets == [PRED]
    assert h.greeted() == []
    await h.hop(NEWCOMER_ID, DBD)  # later hops greet them neither, wherever he follows
    await followed()
    await h.hop(NEWCOMER_ID, PRED)
    await followed()
    assert h.session.targets == [PRED, DBD, PRED]
    assert h.greeted() == []


async def test_his_arrival_greets_nobody_once_he_has_left_that_channel(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    connector = VoiceChannelConnector(None)
    session = VoiceSession(connector, grace_seconds=60, connect_timeout=15.0, max_attempts=1)
    h = make_harness(one_clip, session=session)
    connector.handshake = asyncio.Event()
    joined = h.clock()
    await h.arrive(MEMBER_ID, WF)  # Amir is out of voice: he goes to them
    await settle()
    assert connector.joined == [WF]
    leave = asyncio.create_task(session.disconnect())  # a leave waits for his join to finish
    await settle()
    connector.handshake.set()
    await asyncio.wait_for(leave, 1.0)
    await followed()
    assert session.channel_id is None
    assert h.greeted() == []  # not greeted, and not met either...
    h.clock.advance(5)
    await h.arrive(OTHER_ID, WF)  # someone else comes: he goes back to them
    await followed()
    assert connector.joined == [WF, WF]
    assert h.queue.offered == [  # ...so his return greets them, from their joins
        GreetingJob(MEMBER_ID, joined),
        GreetingJob(OTHER_ID, joined + 5),
    ]


async def test_a_rejoin_within_the_window_is_not_greeted_anywhere(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    h = make_harness(one_clip)
    amir_with(h, WF, OTHER_ID)
    await h.arrive(MEMBER_ID, WF)  # greeted
    await h.depart(MEMBER_ID)
    h.clock.advance(60)
    await h.arrive(MEMBER_ID, WF)  # back within 2 minutes: logged, not greeted
    await followed()
    await h.depart(MEMBER_ID)
    await h.arrive(MEMBER_ID, PRED)  # again, elsewhere this time
    await followed()
    await h.depart(OTHER_ID)  # Warframe empties: Amir goes to them
    await followed()
    assert h.session.targets == [PRED]
    assert h.greeted() == [MEMBER_ID]  # the first greeting only


async def test_rejoin_greeted_when_configured(make_harness: MakeHarness, one_clip: GreetingLibrary):
    h = make_harness(one_clip, greet_on_rejoin=True)
    h.census.put(WF, MEMBER_ID)
    await h.depart(MEMBER_ID)
    await h.arrive(MEMBER_ID, WF)
    async with h.running():
        await asyncio.wait_for(h.player.finished.wait(), 1.0)
    assert h.greeted() == [MEMBER_ID]
    assert len(h.player.played) == 1
    assert h.session.ensure_calls == 1


async def test_rejoin_announced_not_greeted(make_harness: MakeHarness, one_clip: GreetingLibrary):
    h = make_harness(one_clip)
    h.census.put(WF, MEMBER_ID)
    await h.depart(MEMBER_ID)
    await h.arrive(MEMBER_ID, WF)  # back within the rejoin window
    await followed()
    async with h.running():
        await asyncio.wait_for(h.writer.sent.wait(), 1.0)
        await settle()
    assert h.writer.texts == [
        "\n".join(
            [persona.voice_leave_line(MEMBER, CHANNEL), persona.voice_join_line(MEMBER, CHANNEL)]
        )
    ]
    assert h.greeted() == []
    assert h.player.attempts == []
    assert h.session.targets == [WF]  # not greeted, but Amir still joins them, quietly


@pytest.mark.parametrize("waited, greeted", [(29.0, True), (30.0, True), (31.0, False)])
async def test_a_slow_connect_greets_only_within_the_window(
    make_harness: MakeHarness,
    one_clip: GreetingLibrary,
    caplog: pytest.LogCaptureFixture,
    waited: float,
    greeted: bool,
):
    h = make_harness(one_clip)  # the default greeting_expiry_seconds is 30
    h.session.gate = asyncio.Event()
    with caplog.at_level(logging.DEBUG, logger="amir.voice.companion"):
        async with h.running():
            await h.arrive(MEMBER_ID, WF)
            await settle()
            assert h.session.ensure_calls == 1
            h.clock.advance(waited)  # connect attempts and backoff take this long
            h.session.gate.set()
            await followed()
            await settle()
    assert bool(h.player.attempts) is greeted
    assert str(MEMBER_ID) not in caplog.text


# --- the audit log tells a kick from an accident --------------------------------------------


async def test_forced_disconnect_rejoins_quietly(
    make_harness: MakeHarness, one_clip: GreetingLibrary, holds: VoiceHoldRepo
):
    h = make_harness(one_clip)
    h.census.put(WF, MEMBER_ID)  # the kick detector says: not a kick (network etc.)
    async with h.running():
        await h.companion.on_own_disconnect()
        await followed()
    assert h.order[:2] == ["sleep", "kick check"]
    assert h.session.targets == [WF]
    assert h.writer.attempts == 0
    assert h.player.attempts == []
    assert len(h.queue) == 0
    assert await holds.get() is None


class AuditLog:
    """A guild's audit log as discord.py reads it: member_disconnect entries, newest first,
    each with no target, the moderator as `user`, an aggregated count and its created_at."""

    def __init__(self, clock) -> None:
        self.clock = clock
        self.entries: list[SimpleNamespace] = []
        self.gate: asyncio.Event | None = None  # set: every read waits for Discord's answer
        self.reading = asyncio.Event()  # set once a read is waiting at the gate

    def disconnects(self, entry_id: int, *, count: int = 1, age: float = 0.0) -> SimpleNamespace:
        return SimpleNamespace(
            id=entry_id,
            target=None,
            user=SimpleNamespace(bot=False),
            extra=SimpleNamespace(count=count),
            created_at=self.clock.now() - timedelta(seconds=age),
        )

    async def audit_logs(self, *, limit: int | None, action: discord.AuditLogAction) -> Any:
        if self.gate is not None:
            self.reading.set()
            await self.gate.wait()
        for entry in list(self.entries):
            yield entry


def real_detector(guild: Any, clock) -> DiscordKickDetector:
    return DiscordKickDetector(SimpleNamespace(get_guild=lambda guild_id: guild), 2001, clock)


async def start_kick_check(h: Harness) -> tuple["asyncio.Task[None]", asyncio.Event]:
    """Amir drops out of voice; his kick check starts and is held in its wait until the
    returned gate is set."""
    h.session.channel_id = None
    gate = h.sleep.gate = asyncio.Event()
    check = asyncio.create_task(h.companion.on_own_disconnect())
    await asyncio.wait_for(h.sleep.waiting.wait(), 1.0)
    return check, gate


class UnreadableAuditLog:
    """A guild whose audit log Amir may not read: his role lacks View Audit Log."""

    async def audit_logs(self, *, limit: int | None, action: discord.AuditLogAction) -> Any:
        raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "no access")
        yield  # an async generator, as discord.py's


async def test_without_view_audit_log_he_rejoins_with_one_warning(
    make_harness: MakeHarness,
    one_clip: GreetingLibrary,
    holds: VoiceHoldRepo,
    clock,
    caplog: pytest.LogCaptureFixture,
):
    guild = UnreadableAuditLog()
    client = SimpleNamespace(get_guild=lambda guild_id: guild)
    detector = DiscordKickDetector(client, 2001, clock)  # the real one
    h = make_harness(one_clip, kicks=detector)
    h.census.put(WF, MEMBER_ID)
    with caplog.at_level(logging.WARNING):
        await h.companion.on_startup()
        await followed()
        for _ in range(2):
            h.session.channel_id = None  # dropped
            await h.companion.on_own_disconnect()
            await followed()
    assert h.session.targets == [WF] * 3  # startup, then a rejoin after each disconnect
    assert h.companion.hold is None
    assert await holds.get() is None
    assert [r.getMessage() for r in caplog.records] == [AUDIT_LOG_UNREADABLE]


# --- nothing reconnects Amir, or re-reads the audit log, while a kick check is pending ------


async def test_a_greeting_waits_for_the_kick_check_and_a_kick_keeps_him_out(
    make_harness: MakeHarness, one_clip: GreetingLibrary, caplog: pytest.LogCaptureFixture
):
    h = make_harness(one_clip)
    h.census.put(WF, MEMBER_ID, NEWCOMER_ID)
    h.kicks.kicked = True
    with caplog.at_level(logging.DEBUG, logger="amir.voice.companion"):
        check, gate = await start_kick_check(h)
        greeting = asyncio.create_task(h.companion.play_one(h.job(NEWCOMER_ID)))
        await settle()
        assert h.player.attempts == []  # the greeting waits for the check
        gate.set()
        await asyncio.wait_for(asyncio.gather(check, greeting), 1.0)
        await followed()
    assert h.session.ensure_calls == 0
    assert h.player.attempts == []
    assert h.companion.hold is not None and h.companion.hold.reason == "kicked"
    assert "greeting dropped: Amir is held out of voice" in caplog.messages


async def test_a_follow_waits_for_the_kick_check_and_a_kick_keeps_him_out(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    h = make_harness(one_clip)
    h.census.put(WF, MEMBER_ID)
    await h.depart(MEMBER_ID)
    h.kicks.kicked = True
    check, gate = await start_kick_check(h)
    await h.arrive(MEMBER_ID, WF)  # back within the window: a follow is due
    await settle()
    assert h.session.ensure_calls == 0
    gate.set()
    await asyncio.wait_for(check, 1.0)
    await followed()
    assert h.session.ensure_calls == 0
    assert h.companion.hold is not None and h.companion.hold.reason == "kicked"


async def test_an_arrival_during_an_accidental_drops_check_is_greeted_after_it(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    h = make_harness(one_clip)
    h.census.put(WF, MEMBER_ID)  # not a kick: a network drop
    async with h.running():
        check, gate = await start_kick_check(h)
        await h.arrive(NEWCOMER_ID, WF)
        await settle()
        assert h.session.ensure_calls == 0  # the follow waits for the check...
        gate.set()
        await asyncio.wait_for(check, 1.0)
        await asyncio.wait_for(h.player.finished.wait(), 1.0)
    assert h.order.index("kick check") < h.order.index("connect")  # ...then he rejoins
    assert h.companion.hold is None
    assert h.greeted() == [NEWCOMER_ID]  # greeted as he arrives, within their 30 s
    assert len(h.player.played) == 1


async def test_a_greeting_made_stale_by_the_kick_check_is_dropped(
    make_harness: MakeHarness, one_clip: GreetingLibrary, caplog: pytest.LogCaptureFixture
):
    h = make_harness(one_clip)
    h.census.put(WF, MEMBER_ID, NEWCOMER_ID)
    check, gate = await start_kick_check(h)
    greeting = asyncio.create_task(h.companion.play_one(h.job(NEWCOMER_ID)))
    await settle()
    h.clock.advance(31.0)  # the audit log was slow to answer
    with caplog.at_level(logging.INFO, logger="amir.voice.companion"):
        gate.set()
        await asyncio.wait_for(asyncio.gather(check, greeting), 1.0)
        await followed()
    assert h.session.targets == [WF]  # the follow only: the greeting never connects
    assert h.player.attempts == []
    assert "greeting expired before it could play and was dropped" in caplog.messages


async def test_joining_voice_during_a_kick_check_reads_the_audit_log_after_it(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    h = make_harness(one_clip)
    h.census.put(WF, MEMBER_ID)
    check, gate = await start_kick_check(h)
    h.session.channel_id = WF  # a connect already under way lands
    arrival = asyncio.create_task(h.companion.on_own_connect(WF))
    await settle()
    assert h.kicks.snapshots == 0  # never while the check is pending
    gate.set()
    await asyncio.wait_for(asyncio.gather(check, arrival), 1.0)
    await followed()
    assert (h.kicks.asked, h.kicks.snapshots) == (1, 1)


@pytest.mark.parametrize("kick", ["new entry", "raised count"])
async def test_a_reconnect_during_the_kick_check_cannot_hide_the_kick(
    make_harness: MakeHarness, one_clip: GreetingLibrary, clock, kick: str
):
    # The same race against the real detector: a follow due in the wait would reconnect
    # Amir, and his arrival would re-read the audit log, kick included.
    audit = AuditLog(clock)
    audit.entries = [audit.disconnects(11, count=1, age=300)]
    h = make_harness(one_clip, kicks=real_detector(audit, clock))
    amir_with(h, WF, MEMBER_ID, OTHER_ID)
    await h.companion.on_own_connect(WF)  # he joined voice
    await followed()
    clock.advance(300)
    if kick == "new entry":
        audit.entries.insert(0, audit.disconnects(12, age=1))
    else:  # folded into the moderator's earlier entry
        audit.entries[0] = audit.disconnects(11, count=2, age=600)

    check, gate = await start_kick_check(h)
    await h.arrive(NEWCOMER_ID, WF)
    greeting = asyncio.create_task(h.companion.play_one(h.job(NEWCOMER_ID)))
    await settle()
    gate.set()
    await asyncio.wait_for(asyncio.gather(check, greeting), 1.0)
    await followed()

    assert h.companion.hold is not None and h.companion.hold.reason == "kicked"
    assert h.session.ensure_calls == 0


async def test_the_hold_is_in_force_before_it_is_saved(
    make_harness: MakeHarness,
    one_clip: GreetingLibrary,
    holds: VoiceHoldRepo,
    monkeypatch: pytest.MonkeyPatch,
):
    h = make_harness(one_clip)
    h.census.put(WF, MEMBER_ID)
    h.kicks.kicked = True
    in_force: list[VoiceHold | None] = []
    real_save = holds.save

    async def save(hold: VoiceHold) -> None:
        in_force.append(h.companion.hold)  # what a greeting or a follow checking now would see
        await real_save(hold)

    monkeypatch.setattr(holds, "save", save)
    await h.companion.on_own_disconnect()
    assert h.companion.hold is not None and h.companion.hold.reason == "kicked"
    assert in_force == [h.companion.hold]
    assert await holds.get() == h.companion.hold


@pytest.mark.parametrize("reader", ["refresh", "join"])
async def test_a_snapshot_read_under_way_when_a_kick_check_starts_is_discarded(
    make_harness: MakeHarness, one_clip: GreetingLibrary, clock, reader: str
):
    # A race with a read in flight: a snapshot read is in flight (a refresh after someone else left,
    # or Amir's own join) when a moderator kicks him. Discord answers it with the kick in.
    audit = AuditLog(clock)
    audit.entries = [audit.disconnects(11, count=1, age=300)]
    h = make_harness(one_clip, kicks=real_detector(audit, clock))
    amir_with(h, WF, MEMBER_ID, OTHER_ID)
    await h.companion.on_own_connect(WF)  # he joined voice: {11: 1}
    await followed()
    clock.advance(300)
    audit.gate = asyncio.Event()
    if reader == "refresh":
        h.companion.note_member_disconnect()
        h.clock.advance(AUDIT_REFRESH_DELAY_SECONDS)  # due now
        read = asyncio.create_task(h.companion.refresh_snapshot())
    else:
        read = asyncio.create_task(h.companion.on_own_connect(WF))
    await asyncio.wait_for(audit.reading.wait(), 1.0)

    audit.entries[0] = audit.disconnects(11, count=2, age=600)  # the kick, folded into 11
    h.session.channel_id = None
    check = asyncio.create_task(h.companion.on_own_disconnect())
    await settle()  # his own disconnect is in: the kick check has started
    audit.gate.set()  # Discord answers the snapshot read, kick included
    await asyncio.wait_for(asyncio.gather(read, check), 1.0)
    await followed()

    assert h.companion.hold is not None and h.companion.hold.reason == "kicked"
    assert h.session.ensure_calls == 0


# --- a hold whose save failed is saved again, by the companion, until saved or released ---


def failing_saves(
    monkeypatch: pytest.MonkeyPatch, holds: VoiceHoldRepo, failures: int
) -> list[VoiceHold]:
    """holds.save raises OSError for the first `failures` calls; every call is recorded."""
    calls: list[VoiceHold] = []
    real_save = holds.save

    async def save(hold: VoiceHold) -> None:
        calls.append(hold)
        if len(calls) <= failures:
            raise OSError("disk I/O error")
        await real_save(hold)

    monkeypatch.setattr(holds, "save", save)
    return calls


def hold_saver() -> "asyncio.Task[None] | None":
    tasks = [t for t in asyncio.all_tasks() if t.get_name() == "voice-hold-save"]
    return tasks[0] if tasks else None


async def saver_done() -> None:
    saver = hold_saver()
    if saver is not None:
        await asyncio.wait_for(saver, 1.0)


async def kicked_with_retry_waiting(h: Harness) -> asyncio.Event:
    """A moderator kicks Amir; the hold's first save fails (set up by the test), so a retry
    waits at the returned gate. The kick check's own wait goes through."""
    retry = h.sleep.gate = asyncio.Event()
    h.sleep.free = {KICK_CHECK_DELAY_SECONDS}
    h.kicks.kicked = True
    await h.companion.on_own_disconnect()
    await asyncio.wait_for(h.sleep.waiting.wait(), 1.0)
    return retry


SAVE_FAILED = "saving the voice hold failed: OSError; it holds in memory, and saving is retried"


async def test_a_kick_whose_save_failed_is_retried_with_backoff_until_saved(
    make_harness: MakeHarness,
    one_clip: GreetingLibrary,
    holds: VoiceHoldRepo,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    h = make_harness(one_clip)
    h.census.put(WF, MEMBER_ID, OTHER_ID)
    h.kicks.kicked = True
    saves = failing_saves(monkeypatch, holds, failures=8)
    with caplog.at_level(logging.WARNING, logger="amir.voice.companion"):
        await h.companion.on_own_disconnect()  # never raises: the supervisor never restarts it
        hold = h.companion.hold
        assert hold is not None and hold.reason == "kicked"
        await saver_done()
    assert await holds.get() == hold
    assert saves == [hold] * 9  # the same hold: not a new one per attempt
    assert HOLD_SAVE_RETRY_FIRST_SECONDS == 1.0 and HOLD_SAVE_RETRY_MAX_SECONDS == 60.0
    # The kick check's wait, then the backoff between attempts: doubling, capped at 60 s.
    assert h.sleep.delays == [KICK_CHECK_DELAY_SECONDS, 1, 2, 4, 8, 16, 32, 60, 60]
    assert caplog.messages == [SAVE_FAILED] * 8  # one WARNING per failed save
    assert h.kicks.asked == 1  # the disconnect was decided once, never checked again
    assert h.session.ensure_calls == 0


async def test_a_release_stops_the_save_retry_and_sticks(
    make_harness: MakeHarness,
    one_clip: GreetingLibrary,
    holds: VoiceHoldRepo,
    monkeypatch: pytest.MonkeyPatch,
):
    h = make_harness(one_clip)
    saves = failing_saves(monkeypatch, holds, failures=1000)  # the disk stays broken
    retry = await kicked_with_retry_waiting(h)
    assert len(saves) == 1

    assert await h.companion.release_hold() is True  # the owner brings him back
    retry.set()
    await saver_done()

    assert hold_saver() is None
    assert len(saves) == 1  # no save after the release
    assert h.companion.hold is None
    assert await holds.get() is None
    assert h.kicks.asked == 1  # no second kick check


async def test_a_retry_waking_during_a_release_writes_nothing(
    make_harness: MakeHarness,
    one_clip: GreetingLibrary,
    holds: VoiceHoldRepo,
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
):
    h = make_harness(one_clip)
    failing_saves(monkeypatch, holds, failures=1)
    retry = await kicked_with_retry_waiting(h)  # the first save failed; a retry waits
    deleting, finish_delete = asyncio.Event(), asyncio.Event()
    real_clear = holds.clear

    async def clear() -> bool:
        async with db.transaction():  # the DELETE's transaction is open...
            deleting.set()
            await finish_delete.wait()  # ...while the retry wakes
            return await real_clear()

    monkeypatch.setattr(holds, "clear", clear)
    release = asyncio.create_task(h.companion.release_hold())
    await asyncio.wait_for(deleting.wait(), 1.0)
    retry.set()
    await settle()
    finish_delete.set()
    assert await asyncio.wait_for(release, 1.0) is True
    await saver_done()

    assert h.companion.hold is None
    assert await holds.get() is None


async def test_a_save_in_flight_when_a_release_begins_is_deleted_after(
    make_harness: MakeHarness,
    one_clip: GreetingLibrary,
    holds: VoiceHoldRepo,
    monkeypatch: pytest.MonkeyPatch,
):
    h = make_harness(one_clip)
    saves = failing_saves(monkeypatch, holds, failures=1)
    retry = await kicked_with_retry_waiting(h)  # the first save failed; a retry waits
    saving, finish_save = asyncio.Event(), asyncio.Event()
    failing_save = holds.save

    async def slow_save(hold: VoiceHold) -> None:
        saving.set()
        await finish_save.wait()  # the database is slow to take the write
        await failing_save(hold)

    monkeypatch.setattr(holds, "save", slow_save)
    retry.set()
    await asyncio.wait_for(saving.wait(), 1.0)  # the retry is writing
    release = asyncio.create_task(h.companion.release_hold())
    await settle()
    finish_save.set()
    assert await asyncio.wait_for(release, 1.0) is True
    await saver_done()

    assert len(saves) == 2
    assert h.companion.hold is None
    assert await holds.get() is None  # the release's DELETE came after the write


async def test_a_new_hold_set_while_an_old_save_is_in_flight_is_saved(
    make_harness: MakeHarness,
    one_clip: GreetingLibrary,
    holds: VoiceHoldRepo,
    clock,
    monkeypatch: pytest.MonkeyPatch,
):
    h = make_harness(one_clip)
    failing_saves(monkeypatch, holds, failures=1)
    retry = await kicked_with_retry_waiting(h)  # the first save failed; a retry waits
    old = h.companion.hold
    saving, finish_save = asyncio.Event(), asyncio.Event()
    failing_save = holds.save

    async def slow_save(hold: VoiceHold) -> None:
        if hold == old and not saving.is_set():
            saving.set()
            await finish_save.wait()  # the old hold's retry is slow to write
        await failing_save(hold)

    monkeypatch.setattr(holds, "save", slow_save)
    retry.set()
    await asyncio.wait_for(saving.wait(), 1.0)
    release = asyncio.create_task(h.companion.release_hold())  # the owner brings him back...
    await settle()
    clock.advance(600)
    h.sleep.gate = None
    kick = asyncio.create_task(h.companion.on_own_disconnect())  # ...and he is kicked again
    await settle()
    finish_save.set()
    await asyncio.wait_for(asyncio.gather(release, kick), 1.0)
    await saver_done()

    hold = h.companion.hold
    assert hold == VoiceHold(clock.now(), "kicked") != old
    assert await holds.get() == hold  # the new hold is saved, not taken for the old one


async def test_a_newer_holds_retry_replaces_a_released_ones(
    make_harness: MakeHarness,
    one_clip: GreetingLibrary,
    holds: VoiceHoldRepo,
    clock,
    monkeypatch: pytest.MonkeyPatch,
):
    h = make_harness(one_clip)
    saves = failing_saves(monkeypatch, holds, failures=2)
    await kicked_with_retry_waiting(h)  # its save fails: a retry waits
    first = hold_saver()
    await h.companion.release_hold()
    clock.advance(600)
    await h.companion.on_own_disconnect()  # kicked again; this save fails too
    await settle()

    assert first is not None and first.cancelled()  # one retry at a time: the current hold's
    second = hold_saver()
    assert second is not None and second is not first
    assert h.sleep.gate is not None
    h.sleep.gate.set()
    await saver_done()
    assert await holds.get() == h.companion.hold == VoiceHold(clock.now(), "kicked")
    assert len(saves) == 3


async def test_shutdown_stops_the_save_retry(
    make_harness: MakeHarness,
    one_clip: GreetingLibrary,
    holds: VoiceHoldRepo,
    monkeypatch: pytest.MonkeyPatch,
):
    h = make_harness(one_clip)
    failing_saves(monkeypatch, holds, failures=1000)
    await kicked_with_retry_waiting(h)
    assert hold_saver() is not None

    await asyncio.wait_for(h.companion.cancel_background(), 1.0)

    assert hold_saver() is None


async def test_releasing_an_unsaved_hold_still_releases_it(
    make_harness: MakeHarness,
    one_clip: GreetingLibrary,
    holds: VoiceHoldRepo,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
):
    h = make_harness(one_clip)
    failing_saves(monkeypatch, holds, failures=1)
    await kicked_with_retry_waiting(h)  # the retry never comes before the release
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="amir.voice.companion"):
        assert await h.companion.release_hold() is True
        await followed()
    assert caplog.messages == ["voice hold cleared by an admin"]
    assert h.companion.hold is None
    assert await holds.get() is None
    await h.companion.cancel_background()


# --- the audit snapshot follows other members' disconnects -------------------------------


async def test_a_members_disconnect_refreshes_the_snapshot_a_little_later(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    h = make_harness(one_clip)
    h.companion.note_member_disconnect()
    await h.companion.refresh_snapshot()
    assert AUDIT_REFRESH_DELAY_SECONDS == 3.0
    assert h.sleep.delays == [AUDIT_REFRESH_DELAY_SECONDS]
    assert h.kicks.snapshots == 1


async def test_member_disconnects_are_debounced(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    h = make_harness(one_clip)
    h.companion.note_member_disconnect()
    h.clock.advance(2.0)
    h.companion.note_member_disconnect()  # another one, 2 s later
    await h.companion.refresh_snapshot()
    assert h.sleep.delays == [3.0]  # 3 s after the last one
    assert h.kicks.snapshots == 1


async def test_a_disconnect_during_the_refresh_reads_again(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    h = make_harness(one_clip)

    def one_more_disconnect() -> None:
        h.kicks.during_snapshot = None
        h.companion.note_member_disconnect()

    h.kicks.during_snapshot = one_more_disconnect
    h.companion.note_member_disconnect()
    await h.companion.refresh_snapshot()
    assert h.sleep.delays == [3.0, 3.0]
    assert h.kicks.snapshots == 2


async def test_no_refresh_while_a_kick_check_is_pending(
    make_harness: MakeHarness, one_clip: GreetingLibrary, caplog: pytest.LogCaptureFixture
):
    h = make_harness(one_clip)
    check, gate = await start_kick_check(h)
    h.companion.note_member_disconnect()
    h.clock.advance(AUDIT_REFRESH_DELAY_SECONDS)  # due already: no wait
    with caplog.at_level(logging.DEBUG, logger="amir.voice.companion"):
        await asyncio.wait_for(h.companion.refresh_snapshot(), 1.0)
    assert h.kicks.snapshots == 0
    assert "audit log refresh skipped: a kick check is pending" in caplog.messages
    gate.set()
    await asyncio.wait_for(check, 1.0)


async def test_a_moderator_disconnecting_someone_else_is_not_a_kick_later(
    make_harness: MakeHarness, one_clip: GreetingLibrary, holds: VoiceHoldRepo, clock
):
    audit = AuditLog(clock)
    audit.entries = [audit.disconnects(11, count=1, age=3600)]  # from before Amir joined
    h = make_harness(one_clip, kicks=real_detector(audit, clock))
    amir_with(h, WF, MEMBER_ID, OTHER_ID)
    await h.companion.on_own_connect(WF)  # he joins voice
    await followed()
    clock.advance(600)
    # The same moderator disconnects someone else: Discord folds it into the old entry.
    audit.entries[0] = audit.disconnects(11, count=2, age=4200)
    h.companion.note_member_disconnect()
    await h.companion.refresh_snapshot()

    clock.advance(2 * 3600)  # hours later Amir drops out by accident
    h.session.channel_id = None
    await h.companion.on_own_disconnect()
    await followed()

    assert h.companion.hold is None
    assert await holds.get() is None
    assert h.session.targets == [WF]  # he rejoins


# --- a moderator's kick holds Amir out of voice; a drag never does -------------------------


async def test_kick_holds_amir_out(
    make_harness: MakeHarness,
    one_clip: GreetingLibrary,
    holds: VoiceHoldRepo,
    clock,
    caplog: pytest.LogCaptureFixture,
):
    h = make_harness(one_clip)
    h.census.put(PRED, MEMBER_ID, OTHER_ID)  # wherever he was: a kick holds him out of voice
    h.kicks.kicked = True
    with caplog.at_level(logging.INFO, logger="amir.voice.companion"):
        await h.companion.on_own_disconnect()
        await followed()
    assert h.session.ensure_calls == 0  # no rejoin
    hold = VoiceHold(since=clock.now(), reason="kicked")
    assert h.companion.hold == hold
    assert await holds.get() == hold  # persisted
    # Out for sure, even if a connect already under way landed while the audit log was read.
    assert h.order == ["sleep", "kick check", "disconnect"]
    infos = [r.getMessage() for r in caplog.records if r.levelno == logging.INFO]
    assert infos == [
        "a moderator disconnected Amir from voice: he stays out until an admin runs"
        " /amir voice reconnect"
    ]


async def test_arrivals_neither_connect_nor_greet_while_held(
    make_harness: MakeHarness, one_clip: GreetingLibrary, caplog: pytest.LogCaptureFixture
):
    h = make_harness(one_clip)
    h.census.put(WF, MEMBER_ID)
    h.kicks.kicked = True
    await h.companion.on_own_disconnect()
    newcomer = f"<@{NEWCOMER_ID}>"
    with caplog.at_level(logging.DEBUG, logger="amir.voice.companion"):
        async with h.running():
            await h.arrive(NEWCOMER_ID, WF)
            await asyncio.wait_for(h.writer.sent.wait(), 1.0)
            await followed()
    assert h.writer.texts == [persona.voice_join_line(newcomer, CHANNEL)]  # still announced
    assert h.session.ensure_calls == 0
    assert h.greeted() == []
    assert h.player.attempts == []
    assert "not following the crowd: Amir is held out of voice" in caplog.messages
    assert str(NEWCOMER_ID) not in caplog.text


async def test_a_follow_does_not_connect_while_held(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    h = make_harness(one_clip)
    h.census.put(WF, MEMBER_ID)
    h.kicks.kicked = True
    await h.companion.on_own_disconnect()
    await h.depart(MEMBER_ID)
    await h.arrive(MEMBER_ID, WF)
    await h.arrive(OTHER_ID, PRED)
    await followed()
    assert h.session.ensure_calls == 0


async def stored_moved_hold(h: Harness, holds: VoiceHoldRepo, clock) -> VoiceHold:
    """A hold from before this change, when a drag held Amir out: read back at startup."""
    hold = VoiceHold(clock.now(), "moved")
    await holds.save(hold)
    await h.companion.load_hold()
    return hold


async def test_a_stored_moved_hold_still_holds_him_out_until_released(
    make_harness: MakeHarness, one_clip: GreetingLibrary, holds: VoiceHoldRepo, clock
):
    h = make_harness(one_clip)
    hold = await stored_moved_hold(h, holds, clock)
    h.census.put(WF, MEMBER_ID)
    await h.companion.on_startup()
    await h.arrive(OTHER_ID, WF)
    await followed()
    assert h.session.ensure_calls == 0
    assert h.companion.hold == hold

    assert await h.companion.release_hold() is True  # /amir voice reconnect
    await followed()
    assert h.session.targets == [WF]  # back with the people, as soon as it is lifted
    assert await holds.get() is None


async def test_disconnect_while_held_is_left_alone(
    make_harness: MakeHarness, one_clip: GreetingLibrary, holds: VoiceHoldRepo, clock
):
    h = make_harness(one_clip)
    hold = await stored_moved_hold(h, holds, clock)
    h.census.put(WF, MEMBER_ID)
    clock.advance(30)
    await h.companion.on_own_disconnect()
    await followed()
    assert h.order == []  # no wait, no audit log read, no rejoin
    assert await holds.get() == hold


async def test_restart_keeps_the_hold(
    make_harness: MakeHarness,
    one_clip: GreetingLibrary,
    db: Database,
    clock,
    caplog: pytest.LogCaptureFixture,
):
    first = make_harness(one_clip)
    first.census.put(WF, MEMBER_ID)
    first.kicks.kicked = True
    await first.companion.on_own_disconnect()

    # A new process: a new companion and hold record over the same database.
    restarted = make_harness(one_clip, holds=VoiceHoldRepo(db))
    restarted.census.put(WF, MEMBER_ID, OTHER_ID)
    assert restarted.companion.hold is None
    await restarted.companion.load_hold()  # before the gateway connects
    with caplog.at_level(logging.INFO, logger="amir.voice.companion"):
        await restarted.companion.on_startup()
        await followed()

    assert restarted.companion.hold == VoiceHold(clock.now(), "kicked")
    assert restarted.session.ensure_calls == 0
    assert restarted.kicks.snapshots == 1
    assert [r.getMessage() for r in caplog.records] == [
        "Amir is held out of voice (kicked): not joining; /amir voice reconnect brings him back"
    ]


async def test_release_hold_clears_it_once(
    make_harness: MakeHarness,
    one_clip: GreetingLibrary,
    holds: VoiceHoldRepo,
    caplog: pytest.LogCaptureFixture,
):
    h = make_harness(one_clip)
    h.kicks.kicked = True
    await h.companion.on_own_disconnect()
    with caplog.at_level(logging.INFO, logger="amir.voice.companion"):
        assert await h.companion.release_hold() is True
        assert await h.companion.release_hold() is False
        await followed()
    assert h.companion.hold is None
    assert await holds.get() is None
    assert [r.getMessage() for r in caplog.records] == ["voice hold cleared by an admin"]


async def test_after_release_the_next_arrival_connects_and_is_greeted(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    h = make_harness(one_clip)
    h.kicks.kicked = True
    await h.companion.on_own_disconnect()
    await h.companion.release_hold()
    async with h.running():
        await h.arrive(MEMBER_ID, WF)
        await asyncio.wait_for(h.player.finished.wait(), 1.0)
    assert h.session.targets == [WF]
    assert len(h.player.played) == 1


async def test_load_hold_reads_the_record(
    make_harness: MakeHarness, one_clip: GreetingLibrary, holds: VoiceHoldRepo, clock
):
    h = make_harness(one_clip)
    await h.companion.load_hold()
    assert h.companion.hold is None
    hold = VoiceHold(clock.now(), "kicked")
    await holds.save(hold)  # left by an earlier run
    await h.companion.load_hold()
    assert h.companion.hold == hold


# --- play_one: only where Amir already is, never connecting ----------------------------


async def test_playback_failure_tries_another_clip(
    make_harness: MakeHarness, two_clips: GreetingLibrary
):
    h = make_harness(two_clips, player_failures=[PlaybackError("decoder error", clip_fault=True)])
    amir_with(h, WF, MEMBER_ID)
    await h.companion.play_one(h.job())
    first, second = h.player.attempts
    assert first != second
    assert h.player.played == [second]
    assert two_clips.valid_count == 1  # the failing clip is set aside
    assert h.session.ensure_calls == 0


async def test_the_test_greeting_plays_to_no_particular_listener(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    # /amir voice test queues a placeholder member nobody is: it needs only his channel.
    h = make_harness(one_clip)
    amir_with(h, PRED, MEMBER_ID)
    await h.companion.play_one(h.job(TEST_MEMBER_ID))
    assert h.player.listeners == [None]
    assert len(h.player.played) == 1
    assert h.session.ensure_calls == 0


async def test_voice_fault_does_not_mark_the_clip_bad(
    make_harness: MakeHarness, two_clips: GreetingLibrary, caplog: pytest.LogCaptureFixture
):
    h = make_harness(two_clips, player_failures=[PlaybackError("not connected to voice")])
    amir_with(h, WF, MEMBER_ID)
    with caplog.at_level(logging.WARNING):
        await h.companion.play_one(h.job())
    assert len(h.player.attempts) == 1  # no second clip for a voice problem
    assert h.player.played == []
    assert two_clips.valid_count == 2
    assert "not connected to voice" in caplog.text


async def test_second_clip_fault_is_set_aside_without_a_third_try(
    make_harness: MakeHarness, two_clips: GreetingLibrary
):
    faults = [PlaybackError("decoder error", clip_fault=True)] * 2
    h = make_harness(two_clips, player_failures=faults)
    amir_with(h, WF, MEMBER_ID)
    await h.companion.play_one(h.job())
    assert len(set(h.player.attempts)) == 2
    assert h.player.played == []
    assert two_clips.valid_count == 0


async def test_only_clip_fault_has_nothing_else_to_try(
    make_harness: MakeHarness, one_clip: GreetingLibrary, caplog: pytest.LogCaptureFixture
):
    h = make_harness(one_clip, player_failures=[PlaybackError("decoder error", clip_fault=True)])
    amir_with(h, WF, MEMBER_ID)
    with caplog.at_level(logging.WARNING):
        await h.companion.play_one(h.job())
    assert len(h.player.attempts) == 1
    assert one_clip.valid_count == 0
    assert "no other greeting clip" in caplog.text


async def test_no_clip_plays_nothing(
    make_harness: MakeHarness, no_clips: GreetingLibrary, caplog: pytest.LogCaptureFixture
):
    h = make_harness(no_clips)
    amir_with(h, WF, MEMBER_ID, OTHER_ID)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="amir.voice.companion"):
        await h.companion.play_one(h.job())
        await h.companion.play_one(h.job(OTHER_ID))
    assert h.session.ensure_calls == 0
    assert h.player.attempts == []
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 2  # once per occurrence


async def test_greeting_dropped_when_he_is_not_in_voice(
    make_harness: MakeHarness, one_clip: GreetingLibrary, caplog: pytest.LogCaptureFixture
):
    h = make_harness(one_clip)
    h.census.put(WF, MEMBER_ID)
    with caplog.at_level(logging.DEBUG, logger="amir.voice.companion"):
        await h.companion.play_one(h.job())
    assert h.session.ensure_calls == 0  # the greeting worker never connects
    assert h.player.attempts == []
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.DEBUG, "greeting dropped: Amir is not in voice")
    ]


async def test_greeting_dropped_when_they_are_not_with_him(
    make_harness: MakeHarness, one_clip: GreetingLibrary, caplog: pytest.LogCaptureFixture
):
    h = make_harness(one_clip)
    amir_with(h, WF, OTHER_ID)
    h.census.put(PRED, MEMBER_ID)  # they moved on before their greeting came up
    with caplog.at_level(logging.DEBUG, logger="amir.voice.companion"):
        await h.companion.play_one(h.job())
    assert h.player.attempts == []
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.DEBUG, "greeting dropped: they are not in Amir's voice channel")
    ]


async def test_replacement_clip_skipped_once_expired(
    make_harness: MakeHarness, two_clips: GreetingLibrary
):
    h = make_harness(two_clips, player_failures=[PlaybackError("decoder error", clip_fault=True)])
    amir_with(h, WF, MEMBER_ID)
    h.player.during_play = lambda: h.clock.advance(31.0)  # the faulty clip ran long
    await h.companion.play_one(h.job())
    assert len(h.player.attempts) == 1  # no replacement played into a stale arrival
    assert two_clips.valid_count == 1  # the faulty clip is still set aside


# --- the follow task's own failures --------------------------------------------------------


async def test_a_failed_follow_is_logged_not_leaked(
    make_harness: MakeHarness, one_clip: GreetingLibrary, caplog: pytest.LogCaptureFixture
):
    loop = asyncio.get_running_loop()
    reported: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: reported.append(context))
    try:
        h = make_harness(one_clip)
        h.census.error = AttributeError("'NoneType' object has no attribute 'members'")
        with caplog.at_level(logging.WARNING, logger="amir.voice.companion"):
            await h.arrive(MEMBER_ID, WF)  # the event itself never reads the census
            await followed()
        gc.collect()
        await settle()
    finally:
        loop.set_exception_handler(None)
    assert reported == []  # no "Task exception was never retrieved"
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "AttributeError" in warnings[0]
    assert str(MEMBER_ID) not in warnings[0]
    assert h.session.ensure_calls == 0


async def test_a_cancelled_follow_is_not_reported(
    make_harness: MakeHarness, one_clip: GreetingLibrary, caplog: pytest.LogCaptureFixture
):
    loop = asyncio.get_running_loop()
    reported: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: reported.append(context))
    try:
        h = make_harness(one_clip)
        h.session.cancelled = True
        with caplog.at_level(logging.WARNING, logger="amir.voice.companion"):
            await h.arrive(MEMBER_ID, WF)  # the follow is cancelled mid-connect
            await followed()
    finally:
        loop.set_exception_handler(None)
    assert h.session.ensure_calls == 1
    assert reported == []
    assert not caplog.records


# --- privacy ----------------------------------------------------------------------------


async def test_no_member_identity_in_logs(
    make_harness: MakeHarness, two_clips: GreetingLibrary, caplog: pytest.LogCaptureFixture
):
    # Exercise every logging path: a failed clip, a dropped voice-log batch, startup, a settle
    # wait and a move, a drag and its pin ending, AFK, a greeting dropped, a forced disconnect
    # and a failed follow.
    h = make_harness(
        two_clips,
        player_failures=[PlaybackError("decoder error", clip_fault=True)],
        writer_failures=[ChannelUnavailable("no channel id configured for voice_log")] * 3,
    )
    with caplog.at_level(logging.DEBUG):
        async with h.running():
            await h.companion.on_startup()
            await h.arrive(MEMBER_ID, WF)
            await asyncio.wait_for(h.player.finished.wait(), 1.0)
            h.census.put(PRED, OTHER_ID, NEWCOMER_ID)
            await h.arrive(GROUP[0], PRED)
            await followed()
            h.session.channel_id = TRADING
            h.census.put(TRADING, GROUP[1])
            await h.companion.on_own_move(TRADING)
            await h.depart(GROUP[1])
            await followed()
            h.session.channel_id = AFK
            await h.companion.on_own_move(AFK)
            await followed()
            await h.companion.play_one(h.job(MEMBER_ID))
            h.session.channel_id = None
            await h.companion.on_own_disconnect()
            await followed()
            h.census.error = RuntimeError("cache unavailable")
            await h.depart(MEMBER_ID)
            await followed()
            await asyncio.wait_for(h.writer.sent.wait(), 1.0)
            await settle()
    messages = [r.getMessage() for r in caplog.records]
    assert any("dropped" in m for m in messages)
    for member_id in (MEMBER_ID, OTHER_ID, NEWCOMER_ID, *GROUP):
        assert not [m for m in messages if str(member_id) in m or f"<@{member_id}>" in m]
    # A voice event carries ids only, so no display name can reach a log line.
    assert {f.name for f in dataclasses.fields(VoiceEvent)} == {
        "kind",
        "member_id",
        "channel_id",
        "from_channel_id",
    }


async def test_hold_logs_carry_no_member_identity(
    make_harness: MakeHarness, one_clip: GreetingLibrary, caplog: pytest.LogCaptureFixture
):
    # Every hold path: a kick, an arrival and a rejoin while held, a restart while held, the
    # admin's release, a drag after it and a failed kick check.
    h = make_harness(one_clip)
    h.census.put(WF, MEMBER_ID)
    with caplog.at_level(logging.DEBUG):
        async with h.running():
            h.kicks.kicked = True
            await h.companion.on_own_disconnect()
            await h.arrive(NEWCOMER_ID, WF)
            await h.depart(MEMBER_ID)
            await h.arrive(MEMBER_ID, WF)
            await h.companion.on_startup()
            await h.companion.release_hold()
            await followed()
            h.session.channel_id = PRED
            h.census.put(PRED, MEMBER_ID)
            await h.companion.on_own_move(PRED)
            await followed()
            h.kicks.error = RuntimeError("audit log unavailable")
            h.session.channel_id = None
            await h.companion.on_own_disconnect()
            await followed()
            await asyncio.wait_for(h.writer.sent.wait(), 1.0)
            await settle()
    messages = [r.getMessage() for r in caplog.records]
    assert any("held out" in m for m in messages)
    assert any("cleared" in m for m in messages)
    for member_id in (MEMBER_ID, NEWCOMER_ID):
        assert not [m for m in messages if str(member_id) in m]


async def test_humans_present_counts_people_he_could_be_with(
    make_harness: MakeHarness, one_clip: GreetingLibrary
):
    h = make_harness(one_clip)
    h.census.put(WF, MEMBER_ID)
    h.census.put(PRED, OTHER_ID, NEWCOMER_ID)
    h.census.put(TRADING, GROUP[0])  # capped: not a channel he could join
    h.census.put(AFK, GROUP[1])  # parked in AFK
    assert h.companion.humans_present() == 3

    for members in h.census.people.values():
        members.clear()
    assert h.companion.humans_present() == 0
