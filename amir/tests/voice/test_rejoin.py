from amir.voice.rejoin import RejoinTracker

MEMBER = 3001
OTHER_MEMBER = 3002


class FakeClock:
    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def make(window: float = 120.0) -> tuple[RejoinTracker, FakeClock]:
    clock = FakeClock()
    return RejoinTracker(window, clock=clock), clock


def test_rejoin_window():
    tracker, clock = make()
    tracker.record_leave(MEMBER)
    clock.now = 60
    assert tracker.is_rejoin(MEMBER) is True
    clock.now = 121
    assert tracker.is_rejoin(MEMBER) is False
    assert len(tracker) == 0


def test_window_boundary_is_inclusive():
    tracker, clock = make()
    tracker.record_leave(MEMBER)
    clock.now = 120
    assert tracker.is_rejoin(MEMBER) is True


def test_unknown_member_is_not_a_rejoin():
    tracker, _ = make()
    assert tracker.is_rejoin(MEMBER) is False


def test_is_rejoin_does_not_consume_the_record():
    tracker, clock = make()
    tracker.record_leave(MEMBER)
    clock.now = 10
    assert tracker.is_rejoin(MEMBER) is True
    assert tracker.is_rejoin(MEMBER) is True
    assert len(tracker) == 1


def test_record_leave_prunes_expired_entries():
    tracker, clock = make()
    tracker.record_leave(MEMBER)
    clock.now = 500
    tracker.record_leave(OTHER_MEMBER)
    assert len(tracker) == 1
    assert tracker.is_rejoin(MEMBER) is False
    assert tracker.is_rejoin(OTHER_MEMBER) is True


def test_len_prunes():
    tracker, clock = make()
    tracker.record_leave(MEMBER)
    clock.now = 1000
    assert len(tracker) == 0


def test_second_leave_refreshes_the_time():
    tracker, clock = make()
    tracker.record_leave(MEMBER)
    clock.now = 100
    tracker.record_leave(MEMBER)
    clock.now = 200
    assert tracker.is_rejoin(MEMBER) is True
    assert len(tracker) == 1


def test_default_clock_is_monotonic():
    tracker = RejoinTracker(120.0)
    tracker.record_leave(MEMBER)
    assert tracker.is_rejoin(MEMBER) is True
