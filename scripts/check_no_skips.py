"""A pytest plugin scripts/check.sh loads in every stage: a skipped test fails the run.

A skip means a check that never ran (a missing tool, a tree that is not a git checkout), and a
run that passes around it would read as proof it had. Expected failures (xfail) ran and are
left alone.
"""

import pytest

_skipped: list[str] = []


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    if report.skipped and not hasattr(report, "wasxfail"):
        reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else str(report.longrepr)
        _skipped.append(f"{report.nodeid}: {reason.removeprefix('Skipped: ')}")


def pytest_terminal_summary(terminalreporter) -> None:
    if _skipped:
        count = len(_skipped)
        noun = "test fails" if count == 1 else "tests fail"
        terminalreporter.write_line(f"{count} skipped {noun} the check:")
        for line in _skipped:
            terminalreporter.write_line(f"  {line}")


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    if _skipped and exitstatus == pytest.ExitCode.OK:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
