import asyncio
import json
import logging
from collections.abc import Callable

import discord
import pytest

from amir.voice.session import PlaybackError, VoiceConfigError, VoiceSession

GRACE = 0.02  # tiny real grace period for the timer tests
SETTLE = GRACE * 5  # comfortably longer than one grace period
WARFRAME = 3001  # the home voice channel
PRED = 3002  # another voice channel (Arcade)


class FakeConnector:
    """In-memory VoiceConnector: raises queued failures in order, then connects.

    `calls` records each "disconnect" and ("connect", channel_id), in order.
    """

    def __init__(self, failures: list[Exception] | None = None, *, connected: bool = False):
        self.failures = list(failures or [])
        self.channel_id: int | None = WARFRAME if connected else None  # None: out of voice
        self.calls: list[str | tuple[str, int]] = []
        self.connect_timeouts: list[float] = []
        self.disconnects = 0
        self.gate: asyncio.Event | None = None
        self.sticky = False  # disconnect() leaves it connected, so every timer firing counts
        self.disconnect_error: Exception | None = None
        self.on_disconnect: Callable[[], None] | None = None
        self.left = asyncio.Event()  # set when a disconnect() call has finished

    @property
    def connected(self) -> bool:
        return self.channel_id is not None

    async def connect(
        self,
        channel_id: int,
        timeout: float,  # noqa: ASYNC109 - protocol signature
    ) -> None:
        self.calls.append(("connect", channel_id))
        self.connect_timeouts.append(timeout)
        if self.gate is not None:
            await self.gate.wait()
        else:
            await asyncio.sleep(0)
        if self.failures:
            raise self.failures.pop(0)
        self.channel_id = channel_id

    async def disconnect(self) -> bool:
        self.calls.append("disconnect")
        self.disconnects += 1
        if self.on_disconnect is not None:
            self.on_disconnect()
        await asyncio.sleep(0)
        if self.disconnect_error is not None:
            raise self.disconnect_error
        was_in_voice = self.channel_id is not None
        if not self.sticky:
            self.channel_id = None
        self.left.set()
        return was_in_voice

    def is_connected(self) -> bool:
        return self.channel_id is not None

    def current_channel_id(self) -> int | None:
        return self.channel_id


class SleepRecorder:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


def make_session(connector: FakeConnector, sleep: SleepRecorder | None = None, **overrides):
    options = {
        "grace_seconds": GRACE,
        "connect_timeout": 15.0,
        "max_attempts": 5,
        "sleep": sleep or SleepRecorder(),
    }
    options.update(overrides)
    return VoiceSession(connector, **options)


async def test_single_flight_connect():
    connector = FakeConnector()
    connector.gate = asyncio.Event()
    session = make_session(connector)
    first = asyncio.create_task(session.ensure_connected(WARFRAME))
    second = asyncio.create_task(session.ensure_connected(WARFRAME))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert session.state == "connecting"
    connector.gate.set()
    assert await asyncio.gather(first, second) == [True, True]
    assert connector.connect_timeouts == [15.0]
    assert session.state == "connected"


async def test_already_connected_returns_true_without_connecting():
    connector = FakeConnector(connected=True)
    session = make_session(connector)
    assert await session.ensure_connected(WARFRAME) is True
    assert connector.connect_timeouts == []


async def test_backoff_then_success():
    connector = FakeConnector([TimeoutError(), discord.ClientException("voice handshake failed")])
    sleep = SleepRecorder()
    session = make_session(connector, sleep)
    assert await session.ensure_connected(WARFRAME) is True
    assert sleep.delays == [2.0, 4.0]
    assert connector.connect_timeouts == [15.0, 15.0, 15.0]
    assert session.state == "connected"


async def test_backoff_is_capped_at_max_delay():
    connector = FakeConnector([TimeoutError()] * 6)
    sleep = SleepRecorder()
    session = make_session(connector, sleep, max_attempts=6, max_delay=10.0)
    assert await session.ensure_connected(WARFRAME) is False
    assert sleep.delays == [2.0, 4.0, 8.0, 10.0, 10.0]


async def test_gives_up_after_max_attempts():
    connector = FakeConnector([TimeoutError()] * 3)
    sleep = SleepRecorder()
    session = make_session(connector, sleep, max_attempts=3)
    assert await session.ensure_connected(WARFRAME) is False
    assert session.state == "failed"
    assert len(connector.connect_timeouts) == 3
    assert sleep.delays == [2.0, 4.0]


async def test_failed_lasts_until_the_next_attempt():
    connector = FakeConnector([TimeoutError()] * 2)
    session = make_session(connector, max_attempts=2)
    assert await session.ensure_connected(WARFRAME) is False
    assert session.state == "failed"
    assert await session.ensure_connected(WARFRAME) is True
    assert session.state == "connected"


async def test_failures_logged_with_attempt_type_and_message(caplog):
    connector = FakeConnector(
        [TimeoutError(), discord.ClientException("Already connected to a voice channel.")]
    )
    session = make_session(connector, max_attempts=2)
    with caplog.at_level(logging.WARNING, logger="amir.voice.session"):
        assert await session.ensure_connected(WARFRAME) is False
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any(m.endswith("attempt 1/2 failed: TimeoutError") for m in warnings)
    assert any(
        "attempt 2/2" in m and "ClientException: Already connected to a voice channel." in m
        for m in warnings
    )


async def test_config_error_is_not_retried(caplog):
    connector = FakeConnector([VoiceConfigError("channel 1 is a TextChannel, not a voice channel")])
    sleep = SleepRecorder()
    session = make_session(connector, sleep)
    with caplog.at_level(logging.WARNING, logger="amir.voice.session"):
        assert await session.ensure_connected(WARFRAME) is False
    assert session.state == "failed"
    assert len(connector.connect_timeouts) == 1
    assert sleep.delays == []
    assert "is a TextChannel, not a voice channel" in caplog.text


async def test_plain_value_error_is_retried():
    # e.g. a half-received HTTP body while fetching the channel: transient, not misconfiguration
    try:
        json.loads("")
    except json.JSONDecodeError as exc:
        decode_error = exc
    connector = FakeConnector([decode_error])
    sleep = SleepRecorder()
    session = make_session(connector, sleep)
    assert await session.ensure_connected(WARFRAME) is True
    assert len(connector.connect_timeouts) == 2
    assert sleep.delays == [2.0]
    assert session.state == "connected"


def test_config_error_is_a_value_error():
    assert issubclass(VoiceConfigError, ValueError)


def test_playback_error_clip_fault_defaults_to_false():
    assert PlaybackError("not connected to voice").clip_fault is False
    assert PlaybackError("decoder error", clip_fault=True).clip_fault is True


async def test_cancellation_propagates_and_releases_the_lock():
    connector = FakeConnector()
    connector.gate = asyncio.Event()
    session = make_session(connector)
    task = asyncio.create_task(session.ensure_connected(WARFRAME))
    await asyncio.sleep(0)
    assert session.state == "connecting"
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert session.state == "idle"
    connector.gate.set()
    assert await asyncio.wait_for(session.ensure_connected(WARFRAME), 1.0) is True


async def test_grace_cancelled_by_return():
    connector = FakeConnector(connected=True)
    session = make_session(connector)
    session.schedule_disconnect()
    assert session.state == "grace"
    session.cancel_disconnect()
    await asyncio.sleep(SETTLE)
    assert connector.connected
    assert connector.disconnects == 0
    assert session.state == "connected"


async def test_ensure_connected_cancels_pending_grace():
    connector = FakeConnector(connected=True)
    session = make_session(connector)
    session.schedule_disconnect()
    assert await session.ensure_connected(WARFRAME) is True
    await asyncio.sleep(SETTLE)
    assert connector.disconnects == 0
    assert session.state == "connected"


async def test_grace_elapses_disconnects():
    connector = FakeConnector(connected=True)
    session = make_session(connector)
    session.schedule_disconnect()
    assert session.state == "grace"
    await asyncio.wait_for(connector.left.wait(), 1.0)
    assert not connector.connected
    assert connector.disconnects == 1
    assert session.state == "idle"
    assert session.expecting_disconnect is False


async def test_second_schedule_is_a_no_op():
    connector = FakeConnector(connected=True)
    session = make_session(connector)
    session.schedule_disconnect()
    session.schedule_disconnect()
    session.cancel_disconnect()  # one cancel stops the only timer there is
    await asyncio.sleep(SETTLE)
    assert connector.disconnects == 0
    assert session.state == "connected"


async def test_double_schedule_disconnects_once():
    connector = FakeConnector(connected=True)
    connector.sticky = True
    session = make_session(connector)
    session.schedule_disconnect()
    session.schedule_disconnect()
    await asyncio.wait_for(connector.left.wait(), 1.0)
    await asyncio.sleep(SETTLE)
    assert connector.disconnects == 1


async def test_disconnect_when_idle_is_safe(caplog):
    connector = FakeConnector()
    session = make_session(connector)
    with caplog.at_level(logging.INFO, logger="amir.voice.session"):
        await session.disconnect()
    assert session.state == "idle"
    assert session.expecting_disconnect is False
    assert "voice disconnected" not in caplog.text  # there was nothing to leave


async def test_disconnect_leaves_from_another_channel(caplog):
    connector = FakeConnector()
    connector.channel_id = PRED  # dragged out of Warframe, still in voice
    session = make_session(connector)
    assert session.state == "connected"  # connected means in voice, in any channel
    with caplog.at_level(logging.INFO, logger="amir.voice.session"):
        await session.disconnect()
    assert connector.disconnects == 1
    assert not connector.connected
    assert "voice disconnected" in caplog.text


async def test_expecting_disconnect_only_during_disconnect():
    connector = FakeConnector(connected=True)
    session = make_session(connector)
    seen: list[bool] = []
    connector.on_disconnect = lambda: seen.append(session.expecting_disconnect)
    assert session.expecting_disconnect is False
    await session.disconnect()
    assert seen == [True]
    assert session.expecting_disconnect is False
    assert session.state == "idle"


async def test_disconnect_cancels_pending_grace():
    connector = FakeConnector(connected=True)
    connector.sticky = True
    session = make_session(connector)
    session.schedule_disconnect()
    await session.disconnect()
    await asyncio.sleep(SETTLE)
    assert connector.disconnects == 1


async def test_disconnect_failure_is_contained(caplog):
    connector = FakeConnector(connected=True)
    connector.disconnect_error = RuntimeError("voice websocket already closed")
    session = make_session(connector)
    with caplog.at_level(logging.WARNING, logger="amir.voice.session"):
        await session.disconnect()
    assert session.expecting_disconnect is False
    assert any("RuntimeError" in r.getMessage() for r in caplog.records)


async def test_disconnect_waits_for_inflight_connect():
    connector = FakeConnector()
    connector.gate = asyncio.Event()
    session = make_session(connector)
    connecting = asyncio.create_task(session.ensure_connected(WARFRAME))
    await asyncio.sleep(0)
    leaving = asyncio.create_task(session.disconnect())
    await asyncio.sleep(0)
    connector.gate.set()
    assert await connecting is True
    await leaving
    assert connector.disconnects == 1
    assert not connector.connected
    assert session.state == "idle"


async def test_return_cancels_grace_waiting_on_inflight_connect():
    connector = FakeConnector()
    connector.gate = asyncio.Event()
    session = make_session(connector)
    first = asyncio.create_task(session.ensure_connected(WARFRAME))
    await asyncio.sleep(0)
    session.schedule_disconnect()  # the last human left while Amir was still connecting
    await asyncio.sleep(SETTLE)  # the grace period ends while the connect is in flight
    second = asyncio.create_task(session.ensure_connected(WARFRAME))  # a human comes back
    await asyncio.sleep(0)
    connector.gate.set()
    assert await asyncio.gather(first, second) == [True, True]
    await asyncio.sleep(SETTLE)
    assert connector.disconnects == 0
    assert session.state == "connected"


# --- connecting to any channel --------------------------------------------------------------


async def test_ensure_connected_in_another_channel_leaves_then_joins():
    connector = FakeConnector(connected=True)  # in Warframe
    session = make_session(connector)
    leaving: list[bool] = []
    connector.on_disconnect = lambda: leaving.append(session.expecting_disconnect)
    connector.gate = asyncio.Event()  # hold the join open
    moving = asyncio.create_task(session.ensure_connected(PRED))
    await asyncio.wait_for(connector.left.wait(), 1.0)
    await asyncio.sleep(0)
    # A deliberate leave, never a move: the bot's own voice handler sees no kick in it.
    assert leaving == [True]
    assert session.state == "connecting"
    assert session.expecting_disconnect is False  # only while the leave ran
    connector.gate.set()
    assert await moving is True
    assert connector.calls == ["disconnect", ("connect", PRED)]
    assert connector.connect_timeouts == [15.0]
    assert session.channel_id == PRED
    assert session.state == "connected"


async def test_ensure_connected_to_the_current_channel_does_nothing():
    connector = FakeConnector()
    connector.channel_id = PRED
    session = make_session(connector)
    assert await session.ensure_connected(PRED) is True
    assert connector.calls == []
    assert session.channel_id == PRED


async def test_channel_id_follows_the_connector():
    connector = FakeConnector()
    session = make_session(connector)
    assert session.channel_id is None
    assert await session.ensure_connected(WARFRAME) is True
    assert session.channel_id == WARFRAME
    connector.channel_id = PRED  # dragged: the connector is the one that knows
    assert session.channel_id == PRED
    await session.disconnect()
    assert session.channel_id is None


async def test_a_failed_leave_ends_the_change_of_channel(caplog):
    # Joining anyway would leave the old channel to the connector, uncounted: the bot would take
    # that leave for a kick. So he stays where he is, and a later decision tries again.
    connector = FakeConnector(connected=True)
    connector.disconnect_error = RuntimeError("voice websocket already closed")
    session = make_session(connector)
    with caplog.at_level(logging.WARNING, logger="amir.voice.session"):
        assert await session.ensure_connected(PRED) is False
    assert connector.calls == ["disconnect"]  # no connect
    assert session.channel_id == WARFRAME
    assert session.state == "connected"  # not failed: he is still in voice, where he was
    assert session.expecting_disconnect is False
    assert caplog.messages == [
        "voice disconnect failed: RuntimeError",
        "voice channel change abandoned: leaving the current channel failed",
    ]
