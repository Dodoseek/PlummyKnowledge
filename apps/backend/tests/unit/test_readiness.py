from __future__ import annotations

import asyncio
import time

import pytest

from plummy.application.health import CheckReadiness, CheckStatus
from tests.conftest import FakeCheck


async def test_all_checks_ok_means_ready() -> None:
    report = await CheckReadiness([FakeCheck("database"), FakeCheck("redis")]).execute()
    assert report.ready is True
    assert report.checks == {"database": CheckStatus.OK, "redis": CheckStatus.OK}


async def test_single_failure_means_not_ready() -> None:
    checks = [FakeCheck("database", error=ConnectionRefusedError()), FakeCheck("redis")]
    report = await CheckReadiness(checks).execute()
    assert report.ready is False
    assert report.checks == {"database": CheckStatus.FAIL, "redis": CheckStatus.OK}


async def test_slow_check_times_out_as_failure() -> None:
    readiness = CheckReadiness([FakeCheck("database", delay=1.0)], timeout_seconds=0.05)
    started = time.perf_counter()
    report = await readiness.execute()
    assert report.checks == {"database": CheckStatus.FAIL}
    assert time.perf_counter() - started < 0.5


async def test_checks_run_concurrently() -> None:
    checks = [FakeCheck(f"c{i}", delay=0.2) for i in range(3)]
    started = time.perf_counter()
    report = await CheckReadiness(checks, timeout_seconds=1).execute()
    assert report.ready
    assert time.perf_counter() - started < 0.5


async def test_every_check_runs_on_every_call() -> None:
    check = FakeCheck("redis")
    readiness = CheckReadiness([check])
    await readiness.execute()
    await readiness.execute()
    assert check.calls == 2  # no cached result: dependency state is evaluated per request


async def test_no_checks_is_ready() -> None:
    assert (await CheckReadiness([]).execute()).ready is True


async def test_failure_details_are_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    secret_detail = "password authentication failed for user plummy at 10.0.0.5"
    checks = [FakeCheck("database", error=RuntimeError(secret_detail))]
    with caplog.at_level("WARNING"):
        await CheckReadiness(checks).execute()
    assert secret_detail not in caplog.text
    record = caplog.records[-1]
    assert record.__dict__["check"] == "database"
    assert record.__dict__["error_type"] == "RuntimeError"


async def test_cancellation_is_not_swallowed() -> None:
    readiness = CheckReadiness([FakeCheck("database", delay=5)], timeout_seconds=10)
    task = asyncio.create_task(readiness.execute())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


def test_duplicate_check_names_are_rejected() -> None:
    with pytest.raises(ValueError, match="unique"):
        CheckReadiness([FakeCheck("database"), FakeCheck("database")])


@pytest.mark.parametrize("timeout", [0, -1])
def test_non_positive_timeout_is_rejected(timeout: float) -> None:
    with pytest.raises(ValueError, match="timeout"):
        CheckReadiness([], timeout_seconds=timeout)
