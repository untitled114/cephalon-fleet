import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Sequence

import pytest

from amir import persona
from amir.publishing.cards import CONTENT, Card
from amir.publishing.gate import (
    ChannelKey,
    ChannelUnavailable,
    PermissionDenied,
    PublishError,
    SendResult,
    TransientPublishError,
)
from amir.voice.announcer import MAX_PENDING, VoiceAnnouncer

CHANNEL = "<#456>"


def join_line(member_id: int) -> str:
    return persona.voice_join_line(f"<@{member_id}>", CHANNEL)


class FakeWriter:
    """A ChannelWriter that fails with queued errors in order, then records each text sent."""

    def __init__(self, failures: Sequence[Exception] = ()) -> None:
        self.failures = list(failures)
        self.attempts: list[tuple[ChannelKey, str | None]] = []
        self.texts: list[str] = []
        self.finished = 0
        self.changed = asyncio.Event()  # set after every attempt, whatever its outcome

    async def send(
        self,
        channel: ChannelKey,
        *,
        cards: Sequence[Card] = (),
        text: str | None = None,
        role_ping_id: int | None = None,
    ) -> SendResult:
        self.attempts.append((channel, text))
        await asyncio.sleep(0)
        try:
            if self.failures:
                raise self.failures.pop(0)
            assert text is not None
            self.texts.append(text)
            return SendResult("sent", len(self.texts))
        finally:
            self.finished += 1
            self.changed.set()

    async def edit(
        self, channel: ChannelKey, message_id: int, *, cards: Sequence[Card]
    ) -> SendResult:
        raise AssertionError("the voice log never edits")

    async def find_recent(
        self, channel: ChannelKey, identities: set[tuple[str, str, str]], limit: int = 50
    ) -> int | None:
        raise AssertionError("the voice log never searches history")

    async def wait_for_attempts(self, count: int) -> None:
        """Wait until `count` attempts have finished (sent or failed)."""

        async def reached() -> None:
            while self.finished < count:
                self.changed.clear()
                await self.changed.wait()

        await asyncio.wait_for(reached(), 1.0)


class SleepRecorder:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        await asyncio.sleep(0)


@contextlib.asynccontextmanager
async def running(announcer: VoiceAnnouncer) -> AsyncIterator[None]:
    task = asyncio.create_task(announcer.run())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


async def test_announcer_batches():
    writer = FakeWriter()
    announcer = VoiceAnnouncer(writer, sleep=SleepRecorder())
    lines = [join_line(member) for member in (101, 202, 303)]
    for line in lines:
        announcer.announce(line)
    async with running(announcer):
        await writer.wait_for_attempts(1)
        await settle()
    assert writer.texts == ["\n".join(lines)]
    assert writer.attempts[0][0] is ChannelKey.VOICE_LOG


async def test_announce_returns_without_sending():
    writer = FakeWriter()
    announcer = VoiceAnnouncer(writer, sleep=SleepRecorder())
    assert announcer.announce(join_line(101)) is None
    await settle()
    assert writer.attempts == []  # nothing leaves until run() picks it up


async def test_announcer_drops_loudly(caplog: pytest.LogCaptureFixture):
    writer = FakeWriter([ChannelUnavailable("no channel id configured for voice_log")] * 3)
    sleep = SleepRecorder()
    announcer = VoiceAnnouncer(writer, sleep=sleep)
    member = (
        80351110224678912  # Discord's documentation example id: no log line contains it by chance
    )
    announcer.announce(join_line(member))
    with caplog.at_level(logging.DEBUG):
        async with running(announcer):
            await writer.wait_for_attempts(3)
            await settle()
    assert len(writer.attempts) == 3
    assert len({text for _, text in writer.attempts}) == 1  # the same batch every time
    assert writer.texts == []
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "dropped 1" in warnings[0]
    assert "ChannelUnavailable" in warnings[0]
    # min_interval between the attempts, and once more after the batch is given up.
    assert sleep.delays == [2.0, 2.0, 2.0]
    assert all(str(member) not in r.getMessage() for r in caplog.records)


async def test_announcer_retries_then_sends(caplog: pytest.LogCaptureFixture):
    writer = FakeWriter([TransientPublishError("429 Too Many Requests")])
    sleep = SleepRecorder()
    announcer = VoiceAnnouncer(writer, sleep=sleep)
    line = join_line(101)
    announcer.announce(line)
    with caplog.at_level(logging.WARNING):
        async with running(announcer):
            await writer.wait_for_attempts(2)
            await settle()
    assert writer.texts == [line]
    assert sleep.delays[0] == 2.0
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


async def test_announcer_respects_max_attempts(caplog: pytest.LogCaptureFixture):
    writer = FakeWriter([PermissionDenied("403 Forbidden")] * 5)
    announcer = VoiceAnnouncer(writer, max_attempts=5, min_interval=0.5, sleep=SleepRecorder())
    announcer.announce(join_line(101))
    announcer.announce(join_line(202))
    with caplog.at_level(logging.WARNING):
        async with running(announcer):
            await writer.wait_for_attempts(5)
            await settle()
    assert len(writer.attempts) == 5
    assert "dropped 2" in caplog.text


async def test_unexpected_send_error_is_retried_and_dropped_loudly(
    caplog: pytest.LogCaptureFixture,
):
    writer = FakeWriter([ConnectionResetError("connection reset by peer")] * 3)
    announcer = VoiceAnnouncer(writer, sleep=SleepRecorder())
    announcer.announce(join_line(101))
    with caplog.at_level(logging.WARNING):
        async with running(announcer):
            await writer.wait_for_attempts(3)
            await settle()
    assert "dropped 1" in caplog.text
    assert "ConnectionResetError" in caplog.text


async def test_announcer_keeps_going_after_a_drop(caplog: pytest.LogCaptureFixture):
    writer = FakeWriter([PublishError("400 Bad Request")] * 3)
    announcer = VoiceAnnouncer(writer, sleep=SleepRecorder())
    announcer.announce(join_line(101))
    later = join_line(202)
    with caplog.at_level(logging.WARNING):
        async with running(announcer):
            await writer.wait_for_attempts(3)
            announcer.announce(later)
            await writer.wait_for_attempts(4)
            await settle()
    assert writer.texts == [later]
    assert "dropped 1" in caplog.text


async def test_announcer_caps_lines_per_message():
    writer = FakeWriter()
    announcer = VoiceAnnouncer(writer, sleep=SleepRecorder())
    lines = [join_line(1000 + n) for n in range(12)]
    for line in lines:
        announcer.announce(line)
    async with running(announcer):
        await writer.wait_for_attempts(2)
        await settle()
    assert writer.texts == ["\n".join(lines[:10]), "\n".join(lines[10:])]


async def test_announcer_caps_message_length():
    writer = FakeWriter()
    announcer = VoiceAnnouncer(writer, max_lines=1000, sleep=SleepRecorder())
    lines = [join_line(10**17 + n) for n in range(MAX_PENDING)]
    assert len("\n".join(lines)) > CONTENT
    for line in lines:
        announcer.announce(line)
    async with running(announcer):
        await writer.wait_for_attempts(2)
        await settle()
    assert len(writer.texts) == 2
    assert all(len(text) <= CONTENT for text in writer.texts)
    assert "\n".join(writer.texts).split("\n") == lines  # nothing lost, nothing reordered
    # The first message is as full as it can be: one more line would not have fitted.
    first = writer.texts[0].split("\n")
    assert len(writer.texts[0]) + 1 + len(lines[len(first)]) > CONTENT


async def test_overlong_line_is_truncated_to_one_message():
    writer = FakeWriter()
    announcer = VoiceAnnouncer(writer, sleep=SleepRecorder())
    line = " ".join(join_line(101) for _ in range(100))
    assert len(line) > CONTENT
    announcer.announce(line)
    announcer.announce(join_line(202))
    async with running(announcer):
        await writer.wait_for_attempts(2)
        await settle()
    assert len(writer.texts[0]) == CONTENT
    assert writer.texts[1] == join_line(202)


async def test_min_interval_between_sends():
    writer = FakeWriter()
    sleep = SleepRecorder()
    announcer = VoiceAnnouncer(writer, min_interval=3.5, sleep=sleep)
    announcer.announce(join_line(101))
    async with running(announcer):
        await writer.wait_for_attempts(1)
        await settle()
        announcer.announce(join_line(202))
        await writer.wait_for_attempts(2)
        await settle()
    assert len(writer.texts) == 2
    assert sleep.delays == [3.5, 3.5]  # one pause after each message


async def test_backlog_is_capped_dropping_the_oldest(caplog: pytest.LogCaptureFixture):
    assert MAX_PENDING == 50
    writer = FakeWriter()
    announcer = VoiceAnnouncer(writer, sleep=SleepRecorder())
    lines = [join_line(1000 + n) for n in range(MAX_PENDING + 3)]
    with caplog.at_level(logging.WARNING):
        for line in lines:  # e.g. the announcer was stuck behind a long outage
            announcer.announce(line)
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert warnings and all("dropped 1 voice log line(s)" in w for w in warnings)
    assert len(warnings) == 3
    assert all("1000" not in w for w in warnings)  # no member mention in the log
    async with running(announcer):
        await writer.wait_for_attempts(MAX_PENDING // 10)
        await settle()
    sent = "\n".join(writer.texts).split("\n")
    assert sent == lines[3:]  # the newest 50, in order; the three oldest were dropped
