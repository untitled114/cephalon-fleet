"""Voice companion: voice-log lines for every channel, greetings, and where Amir sits.

The announcement comes first and never waits on voice: on_event hands the line to the announcer,
offers any greeting to the queue, and asks the follow task to decide again. No log line here
carries a member id, mention or name; channel ids may be logged.

Rules:

- Where he sits: with whichever voice channel has the most users at the moment. One
  background task, voice-follow, decides after every voice event, his own disconnects, startup,
  a drag and a release, from a census of the server's voice channels
  (amir.voice.follow.choose). When a bigger group forms elsewhere while his own channel still has
  people, he waits the settle time and moves only if it still has more people then; any voice
  change wakes that wait to decide again. With nobody left in any channel he could join, he
  leaves after the grace period.
- His own moves are his decision: leaving to follow the busiest channel is never a drag or a
  kick. He moves only by leaving and then joining, through the session, never
  with discord.py's move_to, so his own moves never look like a drag or a kick.
- A drag pins him: a drag from one channel to another is deliberate and means "stay here with
  us". He stays in that channel, even beside a bigger group, until
  nobody is left in it, and /amir voice reconnect also clears the pin. A drag into AFK, or into a
  channel with nobody in it, pins nothing: he goes back to the crowd. The pin lives in memory.
- A kick holds him out: kicked out of voice by a moderator, he stays out until he is brought back;
  any other disconnect lets him rejoin. A moderator's
  disconnect, read from the audit log, holds him out until an admin runs /amir voice reconnect.
  While held he never joins on his own: arrivals are still announced, but not greeted. Any other
  disconnect is an accident, and he follows the crowd again, quietly. A hold stored with the
  reason "moved" (before this change a drag held him out too) still loads and holds until then.
  The hold is kept in memory, so checking it never suspends, and written through to the database,
  so it survives restarts: load_hold() reads it back before any voice event can arrive.
- One greeting per stay in voice (StayTracker): a stay runs from joining any voice channel to
  leaving voice altogether, so a hop never earns a second greeting, whoever moves. Someone is
  greeted the first time they share a channel with Amir during their stay, if they came into his
  channel, or he came into theirs within the greeting expiry of their joining voice. The welcome
  as he comes in is part of the follow decision that brought him, once his connect is done:
  discord.py reports his join before the voice handshake, while the session knows no channel
  yet, so the bot's report of it only notes the audit log. The greeting worker never connects:
  it plays only in his channel, to someone still there, where he may speak.

While a kick check is pending, nothing reconnects Amir and no audit log snapshot is taken: the
follow task and greetings wait for its answer. A reconnect in the meantime would re-snapshot the
audit log, kick included, and hide the kick. For the same reason a snapshot whose read was under
way when a check began is thrown away.

A hold whose save failed stays in force in memory, and the companion's own background task
(voice-hold-save) saves it again with backoff until it is saved or released. Nothing raises to
the supervisor over it, so a decided disconnect is never checked again. Hold writes take turns
on one lock, and each save re-checks under it that its hold is still the one to save: a release
lifts the hold in memory at once, then deletes it under the lock, so a save never brings back a
hold an admin has lifted.
"""

import asyncio
import logging
import math
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from amir import persona
from amir.clock import Clock
from amir.config.settings import VoiceSettings
from amir.storage.repos import VoiceHold, VoiceHoldRepo
from amir.voice.announcer import VoiceAnnouncer
from amir.voice.classify import VoiceEventKind
from amir.voice.follow import VoiceCensus, choose
from amir.voice.greetings import GreetingLibrary
from amir.voice.queue import GreetingJob, GreetingQueue
from amir.voice.session import KickDetector, PlaybackError, Player, VoiceSession
from amir.voice.stay import StayTracker

log = logging.getLogger(__name__)

# The audit log entry for a kick can trail the disconnect itself: wait this long before reading.
KICK_CHECK_DELAY_SECONDS = 2.0
# Another member's voice disconnect may be a moderator's, which adds to the audit log. Re-read it
# this long after the last such disconnect, so that entry is never later taken for a kick of Amir.
AUDIT_REFRESH_DELAY_SECONDS = 3.0
# A hold whose save failed is saved again after 1 s, then 2, 4 ... at most every 60 s.
HOLD_SAVE_RETRY_FIRST_SECONDS = 1.0
HOLD_SAVE_RETRY_MAX_SECONDS = 60.0


@dataclass(frozen=True)
class VoiceEvent:
    kind: VoiceEventKind
    member_id: int
    channel_id: int  # the channel joined, left, or moved to
    from_channel_id: int | None = None  # set only for a move


@dataclass(frozen=True)
class Pin:
    """A drag put Amir in `channel_id`: he stays there while anyone is in it. `since` is wall
    clock time."""

    channel_id: int
    since: datetime


class VoiceCompanion:
    """`clock` must be the GreetingQueue's and the StayTracker's clock: it stamps jobs and judges
    their expiry, and times the settle wait and the audit log refresh. `wall_clock` stamps a hold
    and a pin. `sleep` waits out the kick-check, refresh and settle delays. `census` reads who is
    in each voice channel now; `settle_seconds` is how long a bigger group elsewhere must last
    before Amir leaves the people he is with."""

    def __init__(
        self,
        *,
        home_channel_id: int,
        session: VoiceSession,
        queue: GreetingQueue,
        library: GreetingLibrary,
        player: Player,
        announcer: VoiceAnnouncer,
        stays: StayTracker,
        settings: VoiceSettings,
        census: Callable[[], VoiceCensus],
        settle_seconds: float,
        kicks: KickDetector,
        holds: VoiceHoldRepo,
        wall_clock: Clock,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[object]] = asyncio.sleep,
    ) -> None:
        self._home_channel_id = home_channel_id
        self._clock = clock
        self._sleep = sleep
        self._kicks = kicks
        self._holds = holds
        self._wall_clock = wall_clock
        self._hold: VoiceHold | None = None
        self._unsaved: VoiceHold | None = None  # the hold in force, while its save has failed
        self._hold_writes = asyncio.Lock()  # saves and the release's delete take turns
        self._kick_check_idle = asyncio.Event()  # cleared while a kick check is pending
        self._kick_check_idle.set()
        self._kick_checks = 0  # how many kick checks have begun
        self._refresh_due = -math.inf  # when the audit log should next be re-read
        self._session = session
        self._queue = queue
        self._library = library
        self._player = player
        self._announcer = announcer
        self._stays = stays
        self._settings = settings
        self._census = census
        self._settle_seconds = settle_seconds
        self._pin: Pin | None = None
        self._follow: asyncio.Task[None] | None = None
        self._changed = asyncio.Event()  # set: something changed since the last decision
        self._decided = asyncio.Event()  # set: voice-follow has nothing to do now (see decided)
        self._decided.set()
        self._settling: tuple[int, float] | None = None  # (channel, when it first had the most)
        self._stopped = False  # shut down: nothing starts the follow task again
        self._hold_saver: asyncio.Task[None] | None = None

    def humans_present(self) -> int:
        """How many people are in the voice channels Amir could join (a count, never who)."""
        return self._census().candidates_with_people()

    def voice_census(self) -> VoiceCensus:
        """Who is in each voice channel now, as the companion reads it."""
        return self._census()

    async def decided(self) -> None:
        """Wait until voice-follow has decided where Amir sits for now: it has finished, or it is
        waiting out a settle time, during which he stays where he is. A connect or a move it
        makes is waited for; the session's backoff bounds that."""
        await self._decided.wait()

    @property
    def hold(self) -> VoiceHold | None:
        """The hold keeping Amir out of voice, if any."""
        return self._hold

    @property
    def pin(self) -> Pin | None:
        """The channel a drag put Amir in, while anyone is still in it."""
        return self._pin

    async def load_hold(self) -> None:
        """Read the hold a moderator left in place before a restart. Run once, before the
        gateway connects."""
        self._hold = await self._holds.get()

    async def on_event(self, event: VoiceEvent) -> None:
        """Announce, keep each stay in voice, greet someone who comes into Amir's channel, then
        decide again where he sits. Never suspends."""
        self._announce(event)
        member = event.member_id
        if event.kind is VoiceEventKind.JOIN:
            self._stays.joined_voice(member)  # only a join from no channel starts a stay
        elif event.kind is VoiceEventKind.LEAVE:
            self._stays.left_voice(member)
        if (
            event.kind is not VoiceEventKind.LEAVE
            and self._staying_in(event.channel_id)
            and self._stays.owed(member)
        ):
            self._queue.offer(GreetingJob(member, self._clock()))
            self._stays.mark_met([member])
        self._decide_again()

    async def on_startup(self) -> None:
        """Note the audit log, so a later kick can be told apart. Then join the biggest group
        quietly (nobody there just arrived, so nobody is owed a greeting), unless a moderator
        holds him out."""
        await self._snapshot()
        if self._hold is not None:
            log.info(
                "Amir is held out of voice (%s): not joining;"
                " /amir voice reconnect brings him back",
                self._hold.reason,
            )
            return
        self._decide_again()

    async def on_own_connect(self, channel_id: int) -> None:
        """Discord reported Amir joining `channel_id`. Note the audit log as it is now, never
        while a kick check is pending: the snapshot would take in the kick it is looking for.

        That is all. discord.py reports his join right after asking for it, before the voice
        handshake is done, so the session may not know his channel yet. Whoever is owed a
        greeting there is welcomed by the follow decision, once his connect is done."""
        await self._kick_check_idle.wait()
        await self._snapshot()

    async def on_own_disconnect(self) -> None:
        """Amir was disconnected without asking. A moderator's kick holds him out; anything else
        (network, voice server, discord.py giving up) is an accident: he follows the crowd again,
        quietly."""
        if self._hold is not None:
            return  # already held: nothing to tell apart, nothing to rejoin
        hold = await self._check_for_kick()
        if hold is not None:
            # Out for sure: a connect already under way before the check may have landed.
            await self._leave_and_save(hold)
            log.info(
                "a moderator disconnected Amir from voice: he stays out until an admin runs"
                " /amir voice reconnect"
            )
            return
        log.info("Amir was disconnected from voice, not by a moderator: he follows the crowd")
        self._decide_again()

    async def on_own_move(self, to_channel_id: int) -> None:
        """Amir was dragged to `to_channel_id` (he never moves himself: he leaves, then joins).
        A drag is deliberate: he stays there with them, and nobody there is greeted for it. A
        drag into AFK, or into a channel with nobody in it, pins nothing and ends an earlier pin:
        he goes back to the crowd. A drag never holds him out."""
        census = self._census()
        people = census.humans_in(to_channel_id)
        self._pin = None
        if to_channel_id == census.afk_channel_id:
            log.info("Amir was moved to the AFK channel: he follows the crowd")
        elif not people:
            log.info(
                "Amir was dragged to voice channel %d, where nobody is: he follows the crowd",
                to_channel_id,
            )
        else:
            self._pin = Pin(to_channel_id, self._wall_clock.now())
            self._stays.mark_met(people)
            log.info(
                "Amir was dragged to voice channel %d: he stays there while anyone is in it",
                to_channel_id,
            )
        self._decide_again()

    def note_member_disconnect(self) -> None:
        """Another member left voice, perhaps disconnected by a moderator: re-read the audit log
        AUDIT_REFRESH_DELAY_SECONDS after the last such disconnect (see refresh_snapshot)."""
        self._refresh_due = self._clock() + AUDIT_REFRESH_DELAY_SECONDS

    async def refresh_snapshot(self) -> None:
        """One-shot: once the latest member disconnect is AUDIT_REFRESH_DELAY_SECONDS old, take
        a new audit log snapshot, unless a kick check is pending. A disconnect noted meanwhile
        brings another round."""
        while True:
            due = self._refresh_due
            delay = due - self._clock()
            if delay > 0:
                await self._sleep(delay)
                continue
            if self._kick_check_idle.is_set():
                await self._snapshot()
            else:
                log.debug("audit log refresh skipped: a kick check is pending")
            if self._refresh_due == due:
                return

    async def release_hold(self) -> bool:
        """An admin brings Amir back: lift the hold and clear the pin, then follow the crowd.
        True if a hold was lifted (saved or not).

        Lifted in memory first, so a save that starts from here on finds nothing to save; the
        delete then waits for a save already under way, so it comes after that write."""
        held = self._hold is not None
        self._hold = self._unsaved = None
        self._pin = None
        try:
            async with self._hold_writes:
                cleared = await self._holds.clear()
        finally:
            self._decide_again()  # lifted means back, even if the delete failed
        if not (held or cleared):
            return False
        log.info("voice hold cleared by an admin")
        return True

    async def cancel_background(self) -> None:
        """Shutdown: stop following the crowd for good, so nothing holds the voice session's lock
        or connects after the bot has closed, and stop the hold-save retry. Wait until both have
        finished."""
        self._stopped = True
        tasks = {t for t in (self._follow, self._hold_saver) if t is not None and not t.done()}
        for task in tasks:
            task.cancel()
        # Nothing decides any more. A follow task cancelled before its first step never reaches
        # its own finally, so decided() is released here.
        self._decided.set()
        if tasks:
            await asyncio.wait(tasks)

    async def play_one(self, job: GreetingJob) -> None:
        """Greeting worker: play one clip in Amir's channel, never connecting; a faulty clip gets
        one replacement. The job is dropped unless no hold is in force, it is still fresh, the
        member is in his channel, and he may speak there. Member id 0 (the test greeting) greets
        nobody in particular, so only his channel counts."""
        await self._kick_check_finished()
        if self._hold is not None:
            log.debug("greeting dropped: Amir is held out of voice")
            return
        if self._expired(job):
            return
        channel_id = self._session.channel_id
        if channel_id is None:
            log.debug("greeting dropped: Amir is not in voice")
            return
        census = self._census()
        if job.member_id and job.member_id not in census.humans_in(channel_id):
            log.debug("greeting dropped: they are not in Amir's voice channel")
            return
        if not census.can_speak_in(channel_id):
            log.debug("greeting dropped: Amir cannot speak in his voice channel")
            return
        clip = self._library.pick()
        if clip is None:
            log.warning("no usable greeting clip: the arrival goes ungreeted")
            return
        if not await self._clip_failed(clip, job) or self._expired(job):
            return
        other = self._library.pick()
        if other is None:
            log.warning("no other greeting clip to try")
            return
        await self._clip_failed(other, job)

    def _announce(self, event: VoiceEvent) -> None:
        member = f"<@{event.member_id}>"
        channel = f"<#{event.channel_id}>"
        if event.kind is VoiceEventKind.JOIN:
            if self._settings.announce_joins:
                self._announcer.announce(persona.voice_join_line(member, channel))
        elif event.kind is VoiceEventKind.LEAVE:
            if self._settings.announce_leaves:
                self._announcer.announce(persona.voice_leave_line(member, channel))
        elif self._settings.announce_moves and event.from_channel_id is not None:
            origin = f"<#{event.from_channel_id}>"
            self._announcer.announce(persona.voice_move_line(member, origin, channel))

    def _staying_in(self, channel_id: int) -> bool:
        """Amir is in `channel_id`, and not on his way out of it."""
        return self._session.channel_id == channel_id and not self._session.expecting_disconnect

    # --- following the crowd ---------------------------------------------------------------

    def _decide_again(self) -> None:
        """Have voice-follow decide where Amir sits: start it, or, while it runs, have it decide
        once more when it is done (a settle wait ends at once). At most one runs at a time."""
        if self._stopped:
            return
        self._changed.set()
        self._decided.clear()
        if self._follow is not None and not self._follow.done():
            return
        task = asyncio.create_task(self._follow_crowd(), name="voice-follow")
        task.add_done_callback(self._follow_done)
        self._follow = task

    def _follow_done(self, task: asyncio.Task[None]) -> None:
        if self._follow is task:
            self._follow = None
        if task.cancelled():
            return
        exc = task.exception()  # retrieved here, so asyncio never reports it as lost
        if exc is not None:
            log.warning("following the crowd failed: %s", type(exc).__name__)

    async def _follow_crowd(self) -> None:
        """voice-follow: decide until nothing has changed since the last decision."""
        try:
            while self._changed.is_set():
                self._changed.clear()
                await self._decide()
        finally:
            self._decided.set()

    async def _decide(self) -> None:
        """One decision from the census as it is now: stay, settle, move or leave."""
        await self._kick_check_finished()
        if self._hold is not None:
            self._settling = None
            log.debug("not following the crowd: Amir is held out of voice")
            return
        census = self._census()
        pin = self._pin
        # A census with no channels at all is one that could not be read: it ends no pin.
        if pin is not None and census.channels and census.count(pin.channel_id) == 0:
            log.info("nobody is left where Amir was dragged: he follows the crowd")
            pin = self._pin = None
        current = self._session.channel_id
        # On his way out (a committed grace leave, say): the channel he is in is not his to stay.
        leaving = self._session.expecting_disconnect
        choice = choose(
            census,
            current=current,
            pin=None if pin is None else pin.channel_id,
            home=self._home_channel_id,
        )
        target = choice.target
        if target is None:
            self._settling = None
            if current is not None and not leaving:
                self._session.schedule_disconnect()
            return
        if target == current and not leaving:
            self._settling = None
            self._session.cancel_disconnect()
            return
        if choice.settle:
            if self._settling is None or self._settling[0] != target:
                self._settling = (target, self._clock())
                log.debug(
                    "more people are in voice channel %d: Amir joins them if they still are in %gs",
                    target,
                    self._settle_seconds,
                )
            remaining = self._settling[1] + self._settle_seconds - self._clock()
            if remaining > 0:
                await self._settle_wait(remaining)
                self._changed.set()  # decide again: the time is up, or something changed
                return
        self._settling = None
        if target == current:
            # Joining waits for the session's lock, so it comes after the leave and rejoins.
            why = "people came as he was leaving"
        else:
            why = "he was dragged there" if choice.pinned else "the most people are there"
        log.info("Amir is joining voice channel %d: %s", target, why)
        if await self._session.ensure_connected(target):
            self._welcome(target)
            return
        if choice.pinned:
            # A full channel, say: staying pinned would keep him out of voice altogether.
            log.info(
                "Amir could not get back into voice channel %d, where he was dragged: he follows"
                " the crowd",
                target,
            )
            self._pin = None
            self._changed.set()
            return
        log.info("Amir did not get into voice channel %d; the next voice change retries", target)

    async def _settle_wait(self, delay: float) -> None:
        """Wait `delay` seconds, or less if something changes first. Meanwhile he stays where he
        is, so that much is decided; deciding again starts when the wait ends."""
        if not self._changed.is_set():
            self._decided.set()
        timer = asyncio.ensure_future(self._sleep(delay))
        changed = asyncio.ensure_future(self._changed.wait())
        try:
            await asyncio.wait((timer, changed), return_when=asyncio.FIRST_COMPLETED)
        finally:
            timer.cancel()
            changed.cancel()
            self._decided.clear()

    # --- greetings -------------------------------------------------------------------------

    def _welcome(self, channel_id: int) -> None:
        """Amir's connect to `channel_id` is done. Greet whoever there is owed a greeting and
        joined voice within the greeting expiry; their job is stamped with that time, so the
        queue's expiry still counts from it. Everyone there is met from now on. On his way out
        again by then (a leave waits for the session), nobody there is greeted or met: they may
        still be greeted when he comes back. Never suspends."""
        if not self._staying_in(channel_id):
            log.debug("Amir is leaving voice channel %d: nobody there is greeted", channel_id)
            return
        people = self._census().humans_in(channel_id)
        for member in sorted(people):
            since = self._stays.fresh_since(member)
            if since is not None and self._stays.owed(member):
                self._queue.offer(GreetingJob(member, since))
        self._stays.mark_met(people)

    def _expired(self, job: GreetingJob) -> bool:
        if self._clock() - job.created_at <= self._settings.greeting_expiry_seconds:
            return False
        log.info("greeting expired before it could play and was dropped")
        return True

    # --- the kick hold ---------------------------------------------------------------------

    async def _check_for_kick(self) -> VoiceHold | None:
        """Wait, then ask the audit log. On a kick, the new hold: it is in force (in memory)
        before anything waiting on the check may go on."""
        self._kick_check_idle.clear()
        self._kick_checks += 1
        try:
            await self._sleep(KICK_CHECK_DELAY_SECONDS)
            if not await self._was_kicked():
                return None
            hold = self._hold = VoiceHold(self._wall_clock.now(), "kicked")
            return hold
        finally:
            self._kick_check_idle.set()

    async def _kick_check_finished(self) -> bool:
        """Wait for a pending kick check to give its answer. True if one was pending."""
        if self._kick_check_idle.is_set():
            return False
        log.debug("waiting for the kick check")
        await self._kick_check_idle.wait()
        return True

    async def _leave_and_save(self, hold: VoiceHold) -> None:
        """Leave voice wherever Amir is, then save the hold already in force, so restarts keep
        it. If the save fails, voice-hold-save tries again in the background; this returns."""
        self._unsaved = hold
        await self._session.disconnect()
        if not await self._try_save(hold):
            self._start_hold_saver(hold)

    def _start_hold_saver(self, hold: VoiceHold) -> None:
        if self._hold_saver is not None:
            self._hold_saver.cancel()  # its hold was released before this one was set
        self._hold_saver = asyncio.create_task(self._keep_saving(hold), name="voice-hold-save")

    async def _keep_saving(self, hold: VoiceHold) -> None:
        """Save `hold` again with backoff until it is saved, or released (or replaced)."""
        delay = HOLD_SAVE_RETRY_FIRST_SECONDS
        while True:
            await self._sleep(delay)
            if await self._try_save(hold):
                return
            delay = min(delay * 2, HOLD_SAVE_RETRY_MAX_SECONDS)

    async def _try_save(self, hold: VoiceHold) -> bool:
        """One attempt to save `hold`. True when nothing is left to save: it is saved now, or
        it is no longer the hold to save (released, under the lock). A failed attempt is one
        WARNING."""
        async with self._hold_writes:
            if self._unsaved is not hold:
                return True
            try:
                await self._holds.save(hold)
            except Exception as exc:
                log.warning(
                    "saving the voice hold failed: %s; it holds in memory, and saving is retried",
                    type(exc).__name__,
                )
                return False
            if self._unsaved is hold:  # not a newer hold, set after a release meanwhile
                self._unsaved = None
            return True

    async def _snapshot(self) -> None:
        """Snapshot the audit log, unless a kick check is pending or begins during the read:
        then the read may hold the very kick the check is looking for."""
        checks = self._kick_checks

        def no_kick_check_since() -> bool:
            if self._kick_check_idle.is_set() and self._kick_checks == checks:
                return True
            log.debug("audit log snapshot dropped: a kick check began while it was read")
            return False

        try:
            await self._kicks.snapshot(keep=no_kick_check_since)
        except Exception as exc:
            log.warning("reading the audit log failed: %s", type(exc).__name__)

    async def _was_kicked(self) -> bool:
        try:
            return await self._kicks.was_kicked()
        except Exception as exc:
            log.warning(
                "kick check failed: %s; treating the disconnect as accidental", type(exc).__name__
            )
            return False

    async def _clip_failed(self, clip: Path, job: GreetingJob) -> bool:
        """Play `clip` to the member `job` greets. True only if the clip itself failed; it is
        then set aside. Member id 0 is /amir voice test's placeholder: no particular listener."""
        try:
            await self._player.play(clip, job.member_id or None)
        except PlaybackError as exc:
            if exc.clip_fault:
                self._library.mark_bad(clip)
                log.warning("greeting clip %s set aside until the next scan: %s", clip.name, exc)
                return True
            log.warning("greeting not played: %s", exc)
        return False
