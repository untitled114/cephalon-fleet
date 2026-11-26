from amir.voice.rejoin import RejoinTracker
from amir.voice.stay import StayTracker

MEMBER = 4001
OTHER_MEMBER = 4002


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def make(greet_on_rejoin: bool = False) -> tuple[StayTracker, FakeClock]:
    clock = FakeClock()
    rejoin = RejoinTracker(120.0, clock=clock)
    tracker = StayTracker(30.0, rejoin, greet_on_rejoin=greet_on_rejoin, clock=clock)
    return tracker, clock


def test_a_new_stay_is_owed_a_greeting():
    tracker, _ = make()
    tracker.joined_voice(MEMBER)
    assert tracker.owed(MEMBER) is True


def test_meeting_amir_settles_the_stay():
    tracker, _ = make()
    tracker.joined_voice(MEMBER)
    tracker.joined_voice(OTHER_MEMBER)
    tracker.mark_met([MEMBER])
    assert tracker.owed(MEMBER) is False
    assert tracker.owed(OTHER_MEMBER) is True


def test_someone_present_since_before_a_restart_is_not_owed():
    tracker, _ = make()
    assert tracker.owed(MEMBER) is False
    tracker.mark_met([MEMBER])
    assert tracker.owed(MEMBER) is False
    assert len(tracker) == 0


def test_leaving_voice_forgets_them():
    tracker, _ = make()
    tracker.joined_voice(MEMBER)
    assert len(tracker) == 1
    tracker.left_voice(MEMBER)
    assert len(tracker) == 0
    assert tracker.owed(MEMBER) is False
    assert tracker.fresh_since(MEMBER) is None


def test_a_rejoin_within_two_minutes_is_not_owed():
    tracker, clock = make()
    tracker.joined_voice(MEMBER)
    tracker.left_voice(MEMBER)
    clock.now = 120
    tracker.joined_voice(MEMBER)
    assert tracker.owed(MEMBER) is False


def test_a_rejoin_after_two_minutes_is_owed():
    tracker, clock = make()
    tracker.joined_voice(MEMBER)
    tracker.left_voice(MEMBER)
    clock.now = 121
    tracker.joined_voice(MEMBER)
    assert tracker.owed(MEMBER) is True


def test_a_rejoin_is_owed_when_greeting_rejoins_is_on():
    tracker, clock = make(greet_on_rejoin=True)
    tracker.joined_voice(MEMBER)
    tracker.left_voice(MEMBER)
    clock.now = 60
    tracker.joined_voice(MEMBER)
    assert tracker.owed(MEMBER) is True


def test_a_met_stay_does_not_carry_into_the_next_one():
    tracker, clock = make(greet_on_rejoin=True)
    tracker.joined_voice(MEMBER)
    tracker.mark_met([MEMBER])
    tracker.left_voice(MEMBER)
    clock.now = 500
    tracker.joined_voice(MEMBER)
    assert tracker.owed(MEMBER) is True


def test_fresh_since_lasts_thirty_seconds():
    tracker, clock = make()
    clock.now = 10.0
    tracker.joined_voice(MEMBER)
    clock.now = 40.0
    assert tracker.fresh_since(MEMBER) == 10.0
    clock.now = 40.001
    assert tracker.fresh_since(MEMBER) is None


def test_fresh_since_is_none_without_a_recorded_stay():
    tracker, _ = make()
    assert tracker.fresh_since(MEMBER) is None


def test_default_clock_is_monotonic():
    tracker = StayTracker(30.0, RejoinTracker(120.0), greet_on_rejoin=False)
    tracker.joined_voice(MEMBER)
    assert tracker.fresh_since(MEMBER) is not None
