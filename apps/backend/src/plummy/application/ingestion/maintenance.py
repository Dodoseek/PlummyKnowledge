"""Maintenance on the ``maintenance`` queue (ARCHITECTURE.md §9.3, DATABASE_SCHEMA.md §8). Each task is
idempotent: a second run changes nothing, so beat may send it again and a crashed run may be redelivered.

- ``PurgeDocument``: chunks, vectors, extracted text and the original of a deleted document;
- ``SweepDeletedDocuments``: purges deletions whose purge message was lost (broker outage);
- ``RequeueJobs``: publishes ``queued`` jobs the broker never accepted;
- ``CleanupQuarantine``: removes quarantined files older than ``QUARANTINE_TTL``.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Final
from uuid import UUID

from plummy.application.ingestion.dispatch import publish
from plummy.application.ports import Clock, JobQueue, JobQueueUnavailableError, UnitOfWorkFactory
from plummy.application.storage import StorageArea, StorageKey, StorageProvider
from plummy.domain.documents import JobStatus

logger = logging.getLogger(__name__)

SWEEP_BATCH: Final = 100
# The API publishes at once; the sweep only handles what is left over after this delay.
PURGE_SWEEP_DELAY: Final = timedelta(minutes=5)
REQUEUE_IDLE: Final = timedelta(minutes=2)


class PurgeDocument:
    def __init__(self, uow: UnitOfWorkFactory, storage: StorageProvider, clock: Clock) -> None:
        self._uow, self._storage, self._clock = uow, storage, clock

    async def execute(self, document_id: UUID) -> bool:
        """``True`` if the document was purged by this run."""
        async with self._uow() as uow:
            document = await uow.documents.get_for_update(document_id)
            if document is None or document.deleted_at is None or document.purged_at is not None:
                return False
            # Deletion cancelled the jobs; a job created before that commit cannot be running any more.
            for job in await uow.ingestion_jobs.unfinished_for_document(document_id):
                job.status, job.error_code, job.finished_at = (
                    JobStatus.CANCELLED,
                    "document_deleted",
                    self._clock.now(),
                )
                await uow.ingestion_jobs.save(job)
            versions = await uow.documents.versions(document_id)
            await uow.chunks.delete_for_document(document_id)
            await uow.commit()
        for version in versions:
            original = StorageKey(version.storage_key)
            await self._storage.delete(StorageArea.QUARANTINE, original)
            await self._storage.delete(StorageArea.DOCUMENTS, original)
            if version.extracted_storage_key is not None:
                await self._storage.delete(StorageArea.DOCUMENTS, StorageKey(version.extracted_storage_key))
        async with self._uow() as uow:
            await uow.documents.mark_purged(document_id, at=self._clock.now())
            await uow.commit()
        logger.info("document purged", extra={"document_id": str(document_id), "versions": len(versions)})
        return True


class SweepDeletedDocuments:
    def __init__(self, uow: UnitOfWorkFactory, queue: JobQueue, clock: Clock) -> None:
        self._uow, self._queue, self._clock = uow, queue, clock

    async def execute(self) -> int:
        async with self._uow() as uow:
            pending = await uow.documents.deleted_unpurged(
                deleted_before=self._clock.now() - PURGE_SWEEP_DELAY, limit=SWEEP_BATCH
            )
        sent = 0
        for document_id in pending:
            try:
                await self._queue.enqueue_purge(document_id)
            except JobQueueUnavailableError:
                break
            sent += 1
        return sent


class RequeueJobs:
    def __init__(self, uow: UnitOfWorkFactory, queue: JobQueue, clock: Clock) -> None:
        self._uow, self._queue, self._clock = uow, queue, clock

    async def execute(self) -> int:
        async with self._uow() as uow:
            jobs = await uow.ingestion_jobs.unpublished(
                idle_since=self._clock.now() - REQUEUE_IDLE, limit=SWEEP_BATCH
            )
        sent = 0
        for job in jobs:
            if not await publish(self._uow, self._queue, job):
                break
            sent += 1
        if sent:
            logger.info("queued jobs published again", extra={"jobs": sent})
        return sent


class CleanupQuarantine:
    def __init__(self, storage: StorageProvider, clock: Clock, *, ttl: timedelta) -> None:
        self._storage, self._clock, self._ttl = storage, clock, ttl

    async def execute(self) -> int:
        """A job whose file expired here fails with ``original_missing`` when it runs."""
        removed = await self._storage.purge_expired(StorageArea.QUARANTINE, self._clock.now() - self._ttl)
        if removed:
            logger.info("expired quarantine files removed", extra={"files": removed})
        return removed
