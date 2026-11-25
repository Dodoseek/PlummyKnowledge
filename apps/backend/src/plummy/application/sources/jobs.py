"""Queuing sync jobs (ADR-020 §8), shared by the API (manual sync, activation), the scheduler and the runner.

At most one job of a source is queued or running. Every writer locks the source row first and then looks for
an active job, so two requests never both insert one; the partial unique index ``sync_jobs_one_active_per_
source`` is the backstop. The job row is the source of truth: it is published to the broker after the commit,
and a job the broker never got is sent again by the scheduler.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Final
from uuid import UUID

from plummy.application.ports import JobQueue, JobQueueUnavailableError, UnitOfWork, UnitOfWorkFactory
from plummy.core.ids import new_id
from plummy.domain.documents import JobStatus
from plummy.domain.knowledge import DataSource
from plummy.domain.sources import MAX_SYNC_ATTEMPTS, SyncJob, SyncMode, SyncTrigger

logger = logging.getLogger(__name__)

CANCELLED_BY_RECONFIGURATION: Final = "source_reconfigured"


def first_mode(source: DataSource) -> SyncMode:
    """Without a saved cursor only a full listing can start (a new source, another folder, a reset)."""
    return SyncMode.INITIAL if source.sync_cursor is None else SyncMode.INCREMENTAL


async def add_sync_job(
    uow: UnitOfWork,
    source: DataSource,
    *,
    trigger: SyncTrigger,
    mode: SyncMode,
    requested_by: UUID | None,
    now: datetime,
) -> SyncJob | None:
    """A new ``queued`` job in the caller's transaction, which holds the lock on ``source``; ``None`` when the
    source already has an active job."""
    if await uow.sync_jobs.active_for_source(source.id) is not None:
        return None
    if source.sync_cursor is None and mode is SyncMode.INCREMENTAL:
        mode = SyncMode.INITIAL
    job = SyncJob(
        id=new_id(),
        data_source_id=source.id,
        trigger=trigger,
        mode=mode,
        status=JobStatus.QUEUED,
        queued_at=now,
        updated_at=now,
        requested_by=requested_by,
        cursor_before=source.sync_cursor,
        max_attempts=MAX_SYNC_ATTEMPTS,
    )
    await uow.sync_jobs.add(job)
    return job


async def publish_sync(uow: UnitOfWorkFactory, queue: JobQueue, job_id: UUID) -> bool:
    """Send a committed job to the ``connectors`` queue; ``False`` if the broker is unavailable."""
    try:
        task_id = await queue.enqueue_sync(job_id)
    except JobQueueUnavailableError:
        logger.warning("sync job not published, broker unavailable", extra={"sync_job_id": str(job_id)})
        return False
    async with uow() as session:
        await session.sync_jobs.set_task_id(job_id, task_id)
        await session.commit()
    return True
