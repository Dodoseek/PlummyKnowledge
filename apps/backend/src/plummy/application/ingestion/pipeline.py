"""Ingestion job runner (ADR-008 §3-4): one pipeline for every source.

The task message carries only the job ID. Every run reloads the job, the document, its source and knowledge
bases and checks their state again; stages are idempotent, so a redelivered or retried job continues safely.

Implemented stages: authorize (2), validate_type and security_check (3-4), store_original (5). Parsing,
chunking and embeddings follow in the next phase steps.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from uuid import UUID

from plummy.application.ingestion.inspection import ArchiveLimits, FileRejectedError, inspect_file
from plummy.application.ports import Clock, UnitOfWorkFactory
from plummy.application.storage import (
    StorageArea,
    StorageKey,
    StorageObjectExistsError,
    StorageObjectNotFoundError,
    StorageProvider,
)
from plummy.domain.documents import (
    Document,
    DocumentStatus,
    DocumentVersion,
    IngestionJob,
    JobResult,
    JobStage,
    JobStatus,
)
from plummy.domain.file_types import DocumentFormat
from plummy.domain.knowledge import DataSourceStatus

logger = logging.getLogger(__name__)

TRANSIENT_ERROR: Final = "transient_error"


class RunOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"  # permanent: not retried
    CANCELLED = "cancelled"  # the document or its source went away
    RETRY = "retry"  # transient: the task is retried with backoff
    DEAD = "dead"  # transient errors exhausted the attempts
    SKIPPED = "skipped"  # unknown or already finished job


class TransientIngestionError(Exception):
    """A temporary failure (storage, database, provider): retried until ``max_attempts``."""


@dataclass(frozen=True, slots=True)
class _Work:
    job: IngestionJob
    document: Document
    version: DocumentVersion


class RunIngestionJob:
    def __init__(
        self,
        uow: UnitOfWorkFactory,
        storage: StorageProvider,
        clock: Clock,
        archive_limits: ArchiveLimits | None = None,
    ) -> None:
        self._uow, self._storage, self._clock = uow, storage, clock
        self._limits = archive_limits or ArchiveLimits()

    async def execute(self, job_id: UUID) -> RunOutcome:
        work = await self._start(job_id)
        if work is None:
            return RunOutcome.SKIPPED
        try:
            if not await self._authorize(work):
                await self._set_document_status(work, DocumentStatus.SKIPPED)
                return await self._finish(work, JobStatus.CANCELLED, error_code="resource_unavailable")
            file_format = await self._inspect(work)
            await self._store_original(work, file_format)
            # Parsing, chunking and embeddings are not wired yet: the stored document waits for them.
            await self._set_document_status(work, DocumentStatus.PENDING)
        except FileRejectedError as exc:
            await self._fail_permanently(work, exc.code, exc.message)
            return RunOutcome.FAILED
        except (TransientIngestionError, OSError) as exc:
            logger.warning(
                "ingestion stage failed, will retry",
                extra={"job_id": str(job_id), "stage": str(work.job.stage), "error_type": type(exc).__name__},
            )
            return await self._transient(work)
        return await self._finish(work, JobStatus.SUCCEEDED, result=JobResult.PROCESSED)

    async def _start(self, job_id: UUID) -> _Work | None:
        async with self._uow() as uow:
            job = await uow.ingestion_jobs.get_for_update(job_id)
            if job is None or job.status.finished or job.document_version_id is None:
                return None
            document = await uow.documents.get(job.document_id)
            version = await uow.documents.get_version(job.document_version_id)
            if document is None or version is None:
                return None
            now = self._clock.now()
            job.status, job.stage, job.attempt = JobStatus.RUNNING, JobStage.AUTHORIZE, job.attempt + 1
            job.started_at = job.started_at or now
            job.error_code = job.error_message = None
            await uow.ingestion_jobs.save(job)
            await uow.documents.set_status(document.id, DocumentStatus.PROCESSING, at=now)
            await uow.commit()
        return _Work(job, document, version)

    async def _authorize(self, work: _Work) -> bool:
        """Stage 2: the document, its source and at least one knowledge base are still live."""
        async with self._uow() as uow:
            document = await uow.documents.get(work.document.id)
            if document is None or document.deleted_at is not None:
                return False
            source = await uow.data_sources.get(document.data_source_id)
            if (
                source is None
                or source.deleted_at is not None
                or source.status is not DataSourceStatus.ACTIVE
            ):
                return False
            return bool(await uow.knowledge_bases.linked_to_source(source.id))

    async def _inspect(self, work: _Work) -> DocumentFormat:
        """Stages 3-4 on whichever copy exists (a retry may find the file already stored)."""
        await self._set_stage(work, JobStage.VALIDATE_TYPE)
        key = StorageKey(work.version.storage_key)
        area = (
            StorageArea.DOCUMENTS
            if await self._storage.exists(StorageArea.DOCUMENTS, key)
            else StorageArea.QUARANTINE
        )
        try:
            async with self._storage.local_path(area, key) as path:
                return inspect_file(path, work.document.filename, self._limits)
        except StorageObjectNotFoundError:
            raise FileRejectedError("original_missing", "The uploaded file is no longer available.") from None

    async def _store_original(self, work: _Work, file_format: DocumentFormat) -> None:
        """Stage 5: move out of quarantine under the same server-generated key."""
        await self._set_stage(work, JobStage.STORE_ORIGINAL)
        key = StorageKey(work.version.storage_key)
        try:
            await self._storage.move(key, StorageArea.QUARANTINE, StorageArea.DOCUMENTS)
        except StorageObjectExistsError:
            await self._storage.delete(StorageArea.QUARANTINE, key)  # moved by an earlier attempt
        except StorageObjectNotFoundError:
            if not await self._storage.exists(StorageArea.DOCUMENTS, key):
                raise
        async with self._uow() as uow:
            await uow.documents.set_mime_type(
                work.document.id, work.version.id, file_format.mime_type, at=self._clock.now()
            )
            await uow.commit()

    async def _set_document_status(self, work: _Work, status: DocumentStatus) -> None:
        async with self._uow() as uow:
            await uow.documents.set_status(work.document.id, status, at=self._clock.now())
            await uow.commit()

    async def _set_stage(self, work: _Work, stage: JobStage) -> None:
        async with self._uow() as uow:
            job = await uow.ingestion_jobs.get_for_update(work.job.id)
            if job is None:
                raise TransientIngestionError("job disappeared")
            job.stage = stage
            await uow.ingestion_jobs.save(job)
            await uow.commit()
        work.job.stage = stage

    async def _fail_permanently(self, work: _Work, code: str, message: str) -> None:
        now = self._clock.now()
        async with self._uow() as uow:
            await uow.documents.fail_version(work.version.id, error_code=code, error_message=message, at=now)
            await uow.commit()
        await self._set_document_status(work, DocumentStatus.FAILED)
        # A rejected file is not kept: quarantine holds only files that may still be processed.
        await self._storage.delete(StorageArea.QUARANTINE, StorageKey(work.version.storage_key))
        await self._finish(work, JobStatus.FAILED, error_code=code, error_message=message)

    async def _transient(self, work: _Work) -> RunOutcome:
        if work.job.attempt >= work.job.max_attempts:
            await self._set_document_status(work, DocumentStatus.FAILED)
            await self._finish(work, JobStatus.DEAD, error_code="attempts_exhausted")
            return RunOutcome.DEAD
        async with self._uow() as uow:
            job = await uow.ingestion_jobs.get_for_update(work.job.id)
            if job is not None:
                job.status, job.error_code = JobStatus.QUEUED, TRANSIENT_ERROR
                await uow.ingestion_jobs.save(job)
                await uow.commit()
        return RunOutcome.RETRY

    async def _finish(
        self,
        work: _Work,
        status: JobStatus,
        *,
        result: JobResult | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> RunOutcome:
        async with self._uow() as uow:
            job = await uow.ingestion_jobs.get_for_update(work.job.id)
            if job is not None:
                job.status, job.result = status, result
                job.error_code, job.error_message = error_code, error_message
                job.finished_at = self._clock.now()
                await uow.ingestion_jobs.save(job)
                await uow.commit()
        return _OUTCOMES[status]


_OUTCOMES: Final = {
    JobStatus.SUCCEEDED: RunOutcome.SUCCEEDED,
    JobStatus.FAILED: RunOutcome.FAILED,
    JobStatus.CANCELLED: RunOutcome.CANCELLED,
    JobStatus.DEAD: RunOutcome.DEAD,
}
