import asyncio
import logging

from amir.bot.tasks import TaskSupervisor


class SleepRecorder:
    """Stands in for asyncio.sleep: records each backoff delay; blocks once `limit` is reached."""

    def __init__(self, limit: int | None = None) -> None:
        self.delays: list[float] = []
        self.limit = limit
        self.reached = asyncio.Event()

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        if self.limit is not None and len(self.delays) >= self.limit:
            self.reached.set()
            await asyncio.Event().wait()  # held here until cancelled
        await asyncio.sleep(0)


async def settle() -> None:
    for _ in range(20):
        await asyncio.sleep(0)


async def test_supervisor_dedupes_and_restarts(caplog):
    sleep = SleepRecorder()
    supervisor = TaskSupervisor(sleep=sleep)
    runs = 0

    async def flaky() -> None:
        nonlocal runs
        runs += 1
        if runs == 1:
            raise RuntimeError("first run fails")
        await asyncio.Event().wait()  # the restarted run keeps going

    with caplog.at_level(logging.ERROR, logger="amir.bot.tasks"):
        assert supervisor.start("worker", flaky) is True
        assert supervisor.start("worker", flaky) is False  # already running
        await settle()
    assert runs == 2
    assert sleep.delays == [1]
    [record] = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert "worker" in record.getMessage()
    assert record.exc_info is not None and record.exc_info[0] is RuntimeError
    assert supervisor.start("worker", flaky) is False  # the restarted run is still running
    await supervisor.cancel_all()


async def test_backoff_doubles_and_caps_at_sixty():
    sleep = SleepRecorder(limit=9)
    supervisor = TaskSupervisor(sleep=sleep)

    async def always_fails() -> None:
        raise ValueError("broken")

    supervisor.start("broken", always_fails)
    await asyncio.wait_for(sleep.reached.wait(), 1)
    assert sleep.delays == [1, 2, 4, 8, 16, 32, 60, 60, 60]
    await supervisor.cancel_all()


async def test_normal_return_is_finished_not_restarted(caplog):
    sleep = SleepRecorder()
    supervisor = TaskSupervisor(sleep=sleep)
    runs = 0

    async def returns_at_once() -> None:  # e.g. NewsPipeline.run with no source enabled
        nonlocal runs
        runs += 1

    with caplog.at_level(logging.INFO, logger="amir.bot.tasks"):
        assert supervisor.start("news", returns_at_once) is True
        await settle()
    assert runs == 1
    assert sleep.delays == []
    assert any(
        r.levelno == logging.INFO and "news" in r.getMessage() and "finished" in r.getMessage()
        for r in caplog.records
    )
    # Not running any more, so the name can be started again.
    assert supervisor.start("news", returns_at_once) is True
    await settle()
    assert runs == 2
    await supervisor.cancel_all()


async def test_a_routine_task_finishes_at_debug(caplog):
    supervisor = TaskSupervisor(sleep=SleepRecorder())

    async def returns_at_once() -> None:  # e.g. the audit log snapshot on every voice join
        pass

    with caplog.at_level(logging.DEBUG, logger="amir.bot.tasks"):
        assert supervisor.start("voice-snapshot", returns_at_once, routine=True) is True
        await settle()
    assert [(r.levelno, r.getMessage()) for r in caplog.records] == [
        (logging.DEBUG, "task voice-snapshot finished")
    ]
    await supervisor.cancel_all()


async def test_a_routine_task_still_logs_its_crash(caplog):
    sleep = SleepRecorder(limit=1)
    supervisor = TaskSupervisor(sleep=sleep)

    async def fails() -> None:
        raise RuntimeError("boom")

    with caplog.at_level(logging.DEBUG, logger="amir.bot.tasks"):
        supervisor.start("voice-move", fails, routine=True)
        await asyncio.wait_for(sleep.reached.wait(), 1)
    assert [r.levelno for r in caplog.records] == [logging.ERROR]
    await supervisor.cancel_all()


async def test_cancel_all_awaits():
    sleep = SleepRecorder(limit=1)
    supervisor = TaskSupervisor(sleep=sleep)
    cleaned: list[str] = []

    def worker(name: str):
        async def run() -> None:
            try:
                await asyncio.Event().wait()
            finally:
                await asyncio.sleep(0)  # cleanup that needs the loop
                cleaned.append(name)

        return run

    async def crashes() -> None:
        raise RuntimeError("crash")

    supervisor.start("a", worker("a"))
    supervisor.start("b", worker("b"))
    supervisor.start("backing-off", crashes)
    await asyncio.wait_for(sleep.reached.wait(), 1)  # "backing-off" now sits in its backoff
    tasks = [t for t in asyncio.all_tasks() if t.get_name() in {"a", "b", "backing-off"}]
    assert len(tasks) == 3

    await supervisor.cancel_all()

    assert sorted(cleaned) == ["a", "b"]  # cancel_all returned only after their cleanup ran
    assert all(t.done() for t in tasks)


async def test_closed_supervisor_starts_nothing():
    supervisor = TaskSupervisor()
    await supervisor.cancel_all()  # shutdown

    async def late() -> None:  # e.g. a voice update arriving while the bot closes
        raise AssertionError("must never run")

    assert supervisor.start("voice-rejoin", late) is False
    await settle()
    assert not [t for t in asyncio.all_tasks() if t.get_name() == "voice-rejoin"]


class Monotonic:
    """Stands in for time.monotonic, advanced by hand."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def test_backoff_resets_after_a_long_run():
    sleep = SleepRecorder(limit=5)
    clock = Monotonic()
    supervisor = TaskSupervisor(sleep=sleep, clock=clock)
    runs = 0

    async def crashes() -> None:
        nonlocal runs
        runs += 1
        if runs == 4:
            clock.now += 10  # this run lasted 10 s, longer than the 8 s delay now due
        raise RuntimeError("crash")

    supervisor.start("worker", crashes)
    await asyncio.wait_for(sleep.reached.wait(), 1)
    assert sleep.delays == [1, 2, 4, 1, 2]
    await supervisor.cancel_all()


async def test_backoff_keeps_growing_when_runs_are_short():
    sleep = SleepRecorder(limit=4)
    clock = Monotonic()
    supervisor = TaskSupervisor(sleep=sleep, clock=clock)

    async def crashes() -> None:
        clock.now += 0.5  # each run is shorter than the delay that follows it
        raise RuntimeError("crash")

    supervisor.start("worker", crashes)
    await asyncio.wait_for(sleep.reached.wait(), 1)
    assert sleep.delays == [1, 2, 4, 8]
    await supervisor.cancel_all()


async def test_running_lists_live_tasks():
    supervisor = TaskSupervisor()
    assert supervisor.running() == []

    async def forever() -> None:
        await asyncio.Event().wait()

    async def finishes() -> None:
        return

    supervisor.start("worker-b", forever)
    supervisor.start("worker-a", forever)
    supervisor.start("finished", finishes)
    await settle()

    assert supervisor.running() == ["worker-a", "worker-b"]
    await supervisor.cancel_all()
    assert supervisor.running() == []
