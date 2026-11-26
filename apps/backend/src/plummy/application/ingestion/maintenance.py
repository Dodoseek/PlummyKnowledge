"""Maintenance on the ``maintenance`` queue (ARCHITECTURE.md §9.3, DATABASE_SCHEMA.md §8). Each task is
idempotent: a second run changes nothing, so beat may send it again and a crashed run may be redelivered.

- ``PurgeDocument``: chunks, vectors, extracted text and the original of a deleted document;
- ``SweepDeletedDocuments``: purges deletions whose purge message was lost (broker outage);
- ``RequeueJobs``: publishes ``queued`` jobs the broker never accepted;
- ``CleanupQuarantine``: removes quarantined files older than ``QUARANTINE_TTL``;
- ``PruneDocumentVersions``: keeps the newest ``KEEP_OUTDATED`` ``outdated`` versions of each live document
  (DATABASE_SCHEMA.md §8, ADR-023 §7) and removes the chunk sets that no version uses.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Final
from uuid import UUID

from plummy.application.ingestion.dispatch import publish
from plummy.application.ports import Clock, JobQueue, JobQueueUnavailableError, UnitOfWorkFactory
from plummy.application.storage import StorageArea, StorageKey, StorageProvider
from plummy.domain.documents import DocumentVersion, JobStatus, VersionStatus

logger = logging.getLogger(__name__)

SWEEP_BATCH: Final = 100
# The API publishes at once; the sweep only handles what is left over after this delay.
PURGE_SWEEP_DELAY: Final = timedelta(minutes=5)
REQUEUE_IDLE: Final = timedelta(minutes=2)
KEEP_OUTDATED: Final = 3
INACTIVE_CHUNKS_BATCH: Final = 5000


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


@dataclass(frozen=True, slots=True)
class PruneReport:
    documents: int = 0
    versions: int = 0
    inactive_chunks: int = 0


class PruneDocumentVersions:
    """Old ``outdated`` versions go with their chunks, vectors, original and extracted text; the current
    version and the newest ``KEEP_OUTDATED`` outdated ones stay (citations to them keep opening the viewer).
    A citation to a removed version keeps its stored snippet; the viewer answers ``404`` for it.

    Removal runs in two steps: the versions become ``deleted`` with their chunks gone (they leave retrieval,
    the viewer and the content lookup), the stored files are deleted, then the rows. A run interrupted between
    the steps is finished by the next one. Documents with a queued or running job are left for a later run, so
    nothing that a job may still read is removed. Removed rows free their checksum: the same content uploaded
    or synced again becomes a new version."""

    def __init__(self, uow: UnitOfWorkFactory, storage: StorageProvider, clock: Clock) -> None:
        self._uow, self._storage, self._clock = uow, storage, clock

    async def execute(self) -> PruneReport:
        async with self._uow() as uow:
            documents = await uow.documents.with_excess_outdated(KEEP_OUTDATED, limit=SWEEP_BATCH)
        pruned = versions = 0
        for document_id in documents:
            removed = await self._prune(document_id)
            if removed:
                pruned, versions = pruned + 1, versions + removed
        async with self._uow() as uow:
            inactive = await uow.chunks.delete_inactive_sets(limit=INACTIVE_CHUNKS_BATCH)
            await uow.commit()
        if versions or inactive:
            logger.info(
                "document versions pruned",
                extra={"documents": pruned, "versions": versions, "inactive_chunks": inactive},
            )
        return PruneReport(documents=pruned, versions=versions, inactive_chunks=inactive)

    async def _prune(self, document_id: UUID) -> int:
        now = self._clock.now()
        async with self._uow() as uow:
            # Document row first, then its jobs: the lock order of the pipeline and of deletion.
            document = await uow.documents.get_for_update(document_id)
            if document is None or document.deleted_at is not None:
                return 0
            if await uow.ingestion_jobs.unfinished_for_document(document_id):
                return 0
            versions = await uow.documents.versions(document_id)  # newest first
            outdated = [
                version
                for version in versions
                if version.status is VersionStatus.OUTDATED and version.id != document.current_version_id
            ]
            targets: list[DocumentVersion] = outdated[KEEP_OUTDATED:] + [
                version for version in versions if version.status is VersionStatus.DELETED
            ]
            if not targets:
                return 0
            ids = [version.id for version in targets]
            await uow.documents.mark_versions_deleted(ids, at=now)
            await uow.chunks.delete_for_versions(ids)
            await uow.commit()
        for version in targets:
            original = StorageKey(version.storage_key)
            await self._storage.delete(StorageArea.QUARANTINE, original)
            await self._storage.delete(StorageArea.DOCUMENTS, original)
            if version.extracted_storage_key is not None:
                await self._storage.delete(StorageArea.DOCUMENTS, StorageKey(version.extracted_storage_key))
        async with self._uow() as uow:
            removed = await uow.documents.delete_versions(ids)
            await uow.commit()
        return removed
