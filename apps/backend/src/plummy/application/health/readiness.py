"""Readiness use case: is the instance able to serve requests right now?"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

logger = logging.getLogger(__name__)

DEFAULT_CHECK_TIMEOUT_SECONDS = 2.0


class CheckStatus(StrEnum):
    OK = "ok"
    FAIL = "fail"


class DependencyCheck(Protocol):
    """Port: verifies one required dependency. Raises any exception when the dependency is not usable."""

    @property
    def name(self) -> str: ...

    async def check(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ReadinessReport:
    checks: dict[str, CheckStatus]

    @property
    def ready(self) -> bool:
        return all(status is CheckStatus.OK for status in self.checks.values())


class CheckReadiness:
    """Runs all dependency checks concurrently, each bounded by a timeout.

    Failure details (hosts, SQL errors, credentials) are logged as an exception type only and never
    returned to the caller.
    """

    def __init__(
        self,
        checks: Sequence[DependencyCheck],
        *,
        timeout_seconds: float = DEFAULT_CHECK_TIMEOUT_SECONDS,
    ) -> None:
        names = [check.name for check in checks]
        if len(set(names)) != len(names):
            raise ValueError("dependency check names must be unique")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._checks = tuple(checks)
        self._timeout = timeout_seconds

    async def execute(self) -> ReadinessReport:
        statuses = await asyncio.gather(*(self._run(check) for check in self._checks))
        return ReadinessReport(checks=dict(zip((c.name for c in self._checks), statuses, strict=True)))

    async def _run(self, check: DependencyCheck) -> CheckStatus:
        try:
            async with asyncio.timeout(self._timeout):
                await check.check()
        except Exception as exc:
            logger.warning(
                "readiness check failed",
                extra={"check": check.name, "error_type": type(exc).__name__},
            )
            return CheckStatus.FAIL
        return CheckStatus.OK
