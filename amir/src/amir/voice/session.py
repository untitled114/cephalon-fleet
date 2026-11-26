"""On-demand voice session: single-flight connect with bounded backoff, and a grace-period leave.

The session connects to whichever voice channel it is asked for. Going from one channel to
another is a deliberate leave and then a fresh join, never a move: discord.py's move_to would
undeafen Amir. expecting_disconnect is True while that leave runs, as for any deliberate leave,
so the bot's voice handler never takes it for a kick. If that leave fails, the change of channel
ends there: joining anyway would leave the old channel to the connector, uncounted, and the bot
would take that leave for a kick.

Also holds the voice ports the companion depends on: VoiceConnector (join or leave a channel),
Player (play one clip) and KickDetector (tell a moderator's kick from any other disconnect).
The discord.py implementations live in discord_adapter.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Literal, Protocol

log = logging.getLogger(__name__)

SessionState = Literal["idle", "connecting", "connected", "grace", "failed"]


class VoiceConfigError(ValueError):
    """The configured voice channel cannot be used (e.g. it is not a voice channel).

    Retrying cannot fix it, so the session fails fast on it. Any other ValueError (such as a
    JSONDecodeError from a half-received response) is retried like other connect failures.
    """


class VoiceConnector(Protocol):
    """connect() joins `channel_id`, and raises VoiceConfigError for a misconfiguration that
    retrying cannot fix.

    is_connected() means connected to voice, in any channel; current_channel_id() is that
    channel, or None. disconnect() leaves voice in whatever channel Amir is in and returns True
    if he was in voice.
    """

    async def connect(
        self,
        channel_id: int,
        timeout: float,  # noqa: ASYNC109 - the VoiceConnector protocol takes a timeout
    ) -> None: ...

    async def disconnect(self) -> bool: ...

    def is_connected(self) -> bool: ...

    def current_channel_id(self) -> int | None: ...


class PlaybackError(Exception):
    """A clip could not be played to the end.

    `clip_fault` is True only when the clip itself failed (a decoding or audio-source error),
    so it should be set aside. Voice problems (not connected, already playing, a timeout)
    leave it False: the clip is fine.
    """

    clip_fault: bool = False

    def __init__(self, message: str = "", *, clip_fault: bool = False) -> None:
        super().__init__(message)
        self.clip_fault = clip_fault


class Player(Protocol):
    async def play(self, path: Path, listener_id: int | None) -> None:
        """Play `path` to the end, once the member it greets can hear it. With no listener (a
        test greeting), once Amir's own side of the call's encryption is ready."""


class KickDetector(Protocol):
    """Tells a moderator's kick from any other disconnect, from the guild's audit log.

    snapshot() notes what the audit log holds now; Amir takes one whenever he joins voice. If
    `keep` is given, it is asked once the read is done, and a False answer leaves the earlier
    snapshot in place. was_kicked() is asked after he was disconnected without asking: True only
    when a moderator has disconnected a member since that snapshot. An audit log that can't be
    read is never a kick, and neither method raises for it.
    """

    async def snapshot(self, keep: Callable[[], bool] | None = None) -> None: ...

    async def was_kicked(self) -> bool: ...


class VoiceSession:
    def __init__(
        self,
        connector: VoiceConnector,
        *,
        grace_seconds: float,
        connect_timeout: float,
        max_attempts: int,
        base_delay: float = 2.0,
        max_delay: float = 60.0,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        self._connector = connector
        self._grace_seconds = grace_seconds
        self._connect_timeout = connect_timeout
        self._max_attempts = max_attempts
        self._base_delay = base_delay
        self._max_delay = max_delay
        self._sleep = sleep
        # Serialises connects and deliberate disconnects so neither interleaves with the other.
        self._lock = asyncio.Lock()
        self._grace_task: asyncio.Task[None] | None = None
        self._connecting = False
        self._failed = False
        self._disconnecting = 0

    @property
    def state(self) -> SessionState:
        if self._connecting:
            return "connecting"
        if self._failed:
            return "failed"
        if not self._connector.is_connected():
            return "idle"
        if self._grace_pending():
            return "grace"
        return "connected"

    @property
    def channel_id(self) -> int | None:
        """The voice channel Amir is in, or None when he is out of voice."""
        return self._connector.current_channel_id()

    @property
    def expecting_disconnect(self) -> bool:
        """True while a deliberate leave is running (disconnect(), or the leave that starts a
        change of channel), so its own voice update is ignored."""
        return self._disconnecting > 0

    async def ensure_connected(self, channel_id: int) -> bool:
        """Be in `channel_id`. True at once if he is already there. In another channel he first
        leaves it deliberately, then joins `channel_id` with the usual backoff. A failed leave
        ends it there: False, and he stays where he is."""
        self.cancel_disconnect()
        async with self._lock:
            self._failed = False
            if self._connector.current_channel_id() == channel_id:
                return True
            self._connecting = True
            try:
                if self._connector.is_connected() and not await self._leave():
                    log.warning(
                        "voice channel change abandoned: leaving the current channel failed"
                    )
                    return False
                return await self._connect_with_backoff(channel_id)
            finally:
                self._connecting = False

    async def _connect_with_backoff(self, channel_id: int) -> bool:
        for attempt in range(1, self._max_attempts + 1):
            # Connect errors carry no member data, so their messages are safe to log.
            try:
                await self._connector.connect(channel_id, self._connect_timeout)
            except VoiceConfigError as exc:
                log.warning(
                    "voice connect attempt %d/%d failed, not retrying: %s",
                    attempt,
                    self._max_attempts,
                    _describe(exc),
                )
                self._failed = True
                return False
            except Exception as exc:  # CancelledError is not an Exception: it propagates
                log.warning(
                    "voice connect attempt %d/%d failed: %s",
                    attempt,
                    self._max_attempts,
                    _describe(exc),
                )
            else:
                log.info("voice connected on attempt %d", attempt)
                return True
            if attempt < self._max_attempts:
                await self._sleep(min(self._base_delay * 2 ** (attempt - 1), self._max_delay))
        self._failed = True
        log.warning("voice connect gave up after %d attempts", self._max_attempts)
        return False

    def schedule_disconnect(self) -> None:
        if self._grace_pending():
            return
        self._grace_task = asyncio.create_task(self._leave_after_grace(), name="voice-grace")

    def cancel_disconnect(self) -> None:
        task = self._grace_task
        # From the timer's own disconnect() this is a no-op: the reference stays, so a human
        # who returns while that disconnect still waits for the lock can cancel it.
        if task is None or task is asyncio.current_task():
            return
        self._grace_task = None
        task.cancel()

    async def disconnect(self) -> None:
        self._disconnecting += 1
        try:
            self.cancel_disconnect()
            async with self._lock:
                if self._grace_task is asyncio.current_task():
                    # Committed: from here a returning human waits for the lock and reconnects.
                    self._grace_task = None
                # Always ask the connector, even when not connected: it also clears a voice
                # client left registered by a cancelled connect.
                await self._leave()
        finally:
            self._disconnecting -= 1

    async def _leave(self) -> bool:
        """Leave voice wherever Amir is, deliberately: expecting_disconnect is True meanwhile.
        The caller holds the lock. A failure is logged, never raised: False."""
        self._disconnecting += 1
        try:
            left = await self._connector.disconnect()
        except Exception as exc:
            log.warning("voice disconnect failed: %s", type(exc).__name__)
            return False
        else:
            if left:
                log.info("voice disconnected")
            return True
        finally:
            self._disconnecting -= 1

    async def _leave_after_grace(self) -> None:
        await asyncio.sleep(self._grace_seconds)
        await self.disconnect()

    def _grace_pending(self) -> bool:
        return self._grace_task is not None and not self._grace_task.done()


def _describe(exc: Exception) -> str:
    message = str(exc)
    return f"{type(exc).__name__}: {message}" if message else type(exc).__name__
