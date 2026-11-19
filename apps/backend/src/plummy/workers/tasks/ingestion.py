"""Ingestion tasks (ADR-008). Input adapters only: the use cases reload and re-check all state.

- ``run_job`` — the extract phase (stages 2-7) on the ``ingestion`` queue, in worker-ingestion;
- ``index_job`` — the index phase (stages 8-12) on the ``embeddings`` queue, in the worker with egress.

The message is ``[job_id]``. Attempts are counted in ``ingestion_jobs`` (not by Celery), so a redelivery
after a worker crash and a retry after a transient error follow the same rules.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any, Final
from uuid import UUID

from celery import shared_task

from plummy.application.ingestion.pipeline import RunOutcome
from plummy.composition import indexing_runner, ingestion_runner
from plummy.core.config import get_settings
from plummy.workers.app import INDEXING_TASK, INGESTION_TASK

RETRY_BACKOFF_SECONDS: Final = (10, 30, 90, 270, 600)
# Celery's own retry cap is above the database limit: the job's ``max_attempts`` decides.
CELERY_MAX_RETRIES: Final = 20


async def run_job(job_id: UUID) -> RunOutcome:
    async with ingestion_runner(get_settings()) as runner:
        return await runner.execute(job_id)


async def index_job(job_id: UUID) -> RunOutcome:
    async with indexing_runner(get_settings()) as runner:
        return await runner.execute(job_id)


def _execute(task: Any, job_id: str, run: Callable[[UUID], Coroutine[Any, Any, RunOutcome]]) -> str:
    outcome = asyncio.run(run(UUID(job_id)))
    if outcome is RunOutcome.RETRY:
        retries = int(task.request.retries)
        countdown = RETRY_BACKOFF_SECONDS[min(retries, len(RETRY_BACKOFF_SECONDS) - 1)]
        raise task.retry(countdown=countdown)
    return outcome.value


def _run_job(task: Any, job_id: str) -> str:
    return _execute(task, job_id, run_job)


def _index_job(task: Any, job_id: str) -> str:
    return _execute(task, job_id, index_job)


_LIMITS: Final[dict[str, Any]] = {
    "bind": True,
    "max_retries": CELERY_MAX_RETRIES,
    "soft_time_limit": 30 * 60,
    "time_limit": 31 * 60,
}
run_ingestion_job = shared_task(name=INGESTION_TASK, **_LIMITS)(_run_job)
run_indexing_job = shared_task(name=INDEXING_TASK, **_LIMITS)(_index_job)
