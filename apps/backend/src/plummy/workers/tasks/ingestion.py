"""Ingestion tasks (ADR-008). Input adapter only: the use case reloads and re-checks all state.

The message is ``[job_id]``. Attempts are counted in ``ingestion_jobs`` (not by Celery), so a redelivery
after a worker crash and a retry after a transient error follow the same rules.
"""

from __future__ import annotations

import asyncio
from typing import Any, Final
from uuid import UUID

from celery import shared_task

from plummy.application.ingestion.pipeline import RunIngestionJob, RunOutcome
from plummy.composition import create_parsers, create_storage
from plummy.core.config import get_settings
from plummy.infrastructure.clock import SystemClock
from plummy.infrastructure.db.engine import create_engine, create_session_factory
from plummy.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWorkFactory
from plummy.workers.app import INGESTION_TASK

RETRY_BACKOFF_SECONDS: Final = (10, 30, 90, 270, 600)
# Celery's own retry cap is above the database limit: the job's ``max_attempts`` decides.
CELERY_MAX_RETRIES: Final = 20


async def run_job(job_id: UUID) -> RunOutcome:
    """One run with its own engine: asyncpg pools belong to the event loop of this run."""
    settings = get_settings()
    engine = create_engine(settings, application_name="plummy-worker")
    try:
        runner = RunIngestionJob(
            SqlAlchemyUnitOfWorkFactory(create_session_factory(engine)),
            create_storage(settings),
            SystemClock(),
            create_parsers(),
        )
        return await runner.execute(job_id)
    finally:
        await engine.dispose()


def _run_job(task: Any, job_id: str) -> str:
    outcome = asyncio.run(run_job(UUID(job_id)))
    if outcome is RunOutcome.RETRY:
        retries = int(task.request.retries)
        countdown = RETRY_BACKOFF_SECONDS[min(retries, len(RETRY_BACKOFF_SECONDS) - 1)]
        raise task.retry(countdown=countdown)
    return outcome.value


run_ingestion_job = shared_task(
    name=INGESTION_TASK,
    bind=True,
    max_retries=CELERY_MAX_RETRIES,
    soft_time_limit=30 * 60,
    time_limit=31 * 60,
)(_run_job)
