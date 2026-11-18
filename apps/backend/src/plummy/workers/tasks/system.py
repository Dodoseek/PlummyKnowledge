"""Diagnostic tasks: verify that the worker receives, retries and completes tasks.

``plummy.system.ping`` is used by ``python -m plummy.cli worker check``. It has no side effects, so it is
trivially idempotent. ``simulate_failures`` makes the first N attempts fail with a transient error to exercise
the retry path end to end (bounded, so a caller cannot keep a worker busy).
"""

from __future__ import annotations

from typing import Any

from celery import shared_task

from plummy.workers.errors import TransientJobError

PING_TASK = "plummy.system.ping"
MAX_RETRIES = 3
MAX_SIMULATED_FAILURES = MAX_RETRIES


def _ping(task: Any, simulate_failures: int = 0) -> dict[str, object]:
    if isinstance(simulate_failures, bool) or not isinstance(simulate_failures, int):
        raise TypeError("simulate_failures must be an integer")
    if not 0 <= simulate_failures <= MAX_SIMULATED_FAILURES:
        raise ValueError(f"simulate_failures must be between 0 and {MAX_SIMULATED_FAILURES}")
    attempt = int(task.request.retries) + 1
    if attempt <= simulate_failures:
        raise TransientJobError("simulated transient failure")
    return {"status": "ok", "attempts": attempt}


ping = shared_task(
    name=PING_TASK,
    bind=True,
    autoretry_for=(TransientJobError,),
    max_retries=MAX_RETRIES,
    retry_backoff=1,
    retry_backoff_max=8,
    retry_jitter=True,
    soft_time_limit=20,
    time_limit=30,
)(_ping)
