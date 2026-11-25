"""Sync of external sources (ADR-020 §8) on the ``connectors`` queue, in the ``worker`` (network, secrets).

The message is ``[sync_job_id]``. Attempts are counted in ``sync_jobs``; Celery only schedules the retry.
A rate-limited provider sets the delay (``Retry-After``) when it is longer than the backoff.
"""

from __future__ import annotations

import asyncio
from typing import Any, Final
from uuid import UUID

from celery import shared_task

from plummy.application.sources.sync import SyncRunOutcome, SyncRunResult
from plummy.composition import sync_runner
from plummy.core.config import get_settings
from plummy.workers.app import SYNC_TASK

RETRY_BACKOFF_SECONDS: Final = (30, 120, 600, 1800, 3600)
MAX_RETRY_DELAY_SECONDS: Final = 3600
CELERY_MAX_RETRIES: Final = 20  # above the database limit: the job's ``max_attempts`` decides


async def run_sync(job_id: UUID) -> SyncRunResult:
    async with sync_runner(get_settings()) as runner:
        return await runner.execute(job_id)


def retry_delay(retries: int, retry_after_seconds: float | None) -> int:
    backoff = RETRY_BACKOFF_SECONDS[min(retries, len(RETRY_BACKOFF_SECONDS) - 1)]
    wanted = max(float(backoff), retry_after_seconds or 0.0)
    return int(min(wanted, MAX_RETRY_DELAY_SECONDS))


def _sync(task: Any, job_id: str) -> str:
    result = asyncio.run(run_sync(UUID(job_id)))
    if result.outcome is SyncRunOutcome.RETRY:
        raise task.retry(countdown=retry_delay(int(task.request.retries), result.retry_after_seconds))
    return result.outcome.value


# A full listing of a large folder takes long; the limit stays below the broker's visibility timeout.
run_sync_job = shared_task(
    name=SYNC_TASK, bind=True, max_retries=CELERY_MAX_RETRIES, soft_time_limit=100 * 60, time_limit=105 * 60
)(_sync)
