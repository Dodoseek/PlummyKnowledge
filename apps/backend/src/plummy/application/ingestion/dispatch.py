"""Publishing ingestion jobs to the workers, shared by upload, reindex, retry and the maintenance sweep.

The job row in PostgreSQL is the source of truth. A job is ``queued`` without a task ID until the broker
accepted it; if the broker is down the job stays like that and the sweep (``requeue_jobs``) sends it later.
"""

from __future__ import annotations

import logging
from typing import Final

from plummy.application.auth.throttle import Limit
from plummy.application.errors import RateLimitedError, ServiceUnavailableError
from plummy.application.ports import (
    JobQueue,
    JobQueueUnavailableError,
    RateLimiter,
    RateLimiterUnavailableError,
    UnitOfWorkFactory,
)
from plummy.domain.documents import IngestionJob, JobStage

logger = logging.getLogger(__name__)

# Stages run by the index phase (``embeddings`` queue); the others by worker-ingestion.
INDEX_STAGES: Final = frozenset(
    {JobStage.CHUNK, JobStage.EMBED, JobStage.PERSIST, JobStage.FINALIZE, JobStage.REPORT}
)
# ``expensive`` (API_CONTRACTS.md §5): reindex and retry together, for the whole installation.
EXPENSIVE_PER_HOUR: Final = Limit("expensive:instance:1h", 10, 60 * 60)


async def publish(uow: UnitOfWorkFactory, queue: JobQueue, job: IngestionJob) -> bool:
    """Send the job to the queue of its phase; ``False`` if the broker is unavailable (the sweep retries)."""
    try:
        if job.stage in INDEX_STAGES:
            task_id = await queue.enqueue_indexing(job.id)
        else:
            task_id = await queue.enqueue_ingestion(job.id)
    except JobQueueUnavailableError:
        logger.warning("ingestion job not published, broker unavailable", extra={"job_id": str(job.id)})
        return False
    async with uow() as session:
        await session.ingestion_jobs.set_task_id(job.id, task_id)
        await session.commit()
    return True


async def enforce_expensive_limit(limiter: RateLimiter) -> None:
    limit = EXPENSIVE_PER_HOUR
    try:
        status = await limiter.hit(limit.name, limit=limit.limit, window_seconds=limit.window_seconds)
    except RateLimiterUnavailableError:
        raise ServiceUnavailableError() from None
    if not status.allowed:
        raise RateLimitedError(status.retry_after_seconds)
