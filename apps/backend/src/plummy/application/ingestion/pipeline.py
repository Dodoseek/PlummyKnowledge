"""Ingestion pipeline (ADR-008 §3), run by the workers.

The task message carries only the job ID. Every run reloads the job, the document, its source and knowledge
bases and checks their state again; stages are idempotent, so a redelivered or retried job continues safely.

One job runs in two phases on two queues:

- **extract** (``ingestion`` queue, worker-ingestion without network): authorize (2), validate_type and
  security_check (3-4), store_original (5), parse and normalize (6-7). Then the job is handed off.
- **index** (``embeddings`` queue, the worker that may reach embedding providers): chunk (8), embed (9),
  persist (10), finalize (11) — :mod:`plummy.application.ingestion.indexing`.

``attempt`` counts runs that start the work again (the first run, a retry after a transient error, a
redelivery after a crash); the hand-off between the phases is not an attempt.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Final
from uuid import UUID

from plummy.application.ingestion.inspection import ArchiveLimits, FileRejectedError, inspect_file
from plummy.application.ingestion.parsing import ParseError, ParseLimits, ParserRegistry
from plummy.application.ports import Clock, JobQueue, JobQueueUnavailableError, UnitOfWorkFactory
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
    VersionStatus,
)
from plummy.domain.file_types import DocumentFormat
from plummy.domain.knowledge import DataSourceStatus, KnowledgeBase

logger = logging.getLogger(__name__)

TRANSIENT_ERROR: Final = "transient_error"


class RunOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"  # permanent: not retried
    CANCELLED = "cancelled"  # the document or its source went away
    RETRY = "retry"  # transient: the task is retried with backoff
    DEAD = "dead"  # transient errors exhausted the attempts
    SKIPPED = "skipped"  # unknown or already finished job, or not this phase's turn
    HANDED_OFF = "handed_off"  # extract phase done, the index phase is queued


class Phase(StrEnum):
    EXTRACT = "extract"
    INDEX = "index"


class TransientIngestionError(Exception):
    """A temporary failure (storage, database, broker, provider): retried until ``max_attempts``."""


class PermanentIngestionError(Exception):
    """The document cannot be processed as it is; the job fails without retries."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True, slots=True)
class Work:
    job: IngestionJob
    document: Document
    version: DocumentVersion


class JobRunner:
    """Job lifecycle shared by both phases: start, stage updates, permanent and transient failures."""

    phase: Phase

    def __init__(self, uow: UnitOfWorkFactory, storage: StorageProvider, clock: Clock) -> None:
        self._uow, self._storage, self._clock = uow, storage, clock

    async def execute(self, job_id: UUID) -> RunOutcome:
        started = await self._start(job_id)
        if isinstance(started, RunOutcome):
            return started
        work = started
        try:
            if not await self._linked_knowledge_bases(work):
                await self._set_document_status(work, DocumentStatus.SKIPPED)
                return await self._finish(work, JobStatus.CANCELLED, error_code="resource_unavailable")
            return await self._run(work)
        except (FileRejectedError, ParseError, PermanentIngestionError) as exc:
            await self._fail_permanently(work, exc.code, exc.message)
            return RunOutcome.FAILED
        except (TransientIngestionError, JobQueueUnavailableError, OSError) as exc:
            logger.warning(
                "ingestion stage failed, will retry",
                extra={"job_id": str(job_id), "stage": str(work.job.stage), "error_type": type(exc).__name__},
            )
            return await self._transient(work)

    async def _run(self, work: Work) -> RunOutcome:
        raise NotImplementedError

    def _ready_for_phase(self, version: DocumentVersion) -> bool:
        return True

    async def _start(self, job_id: UUID) -> Work | RunOutcome:
        async with self._uow() as uow:
            job = await uow.ingestion_jobs.get_for_update(job_id)
            if job is None or job.status.finished or job.document_version_id is None:
                return RunOutcome.SKIPPED
            document = await uow.documents.get(job.document_id)
            version = await uow.documents.get_version(job.document_version_id)
            if document is None or version is None:
                return RunOutcome.SKIPPED
            now = self._clock.now()
            if version.status is VersionStatus.READY and version.active_chunk_set is not None:
                # SC-09: the version is already indexed; nothing changes.
                job.status, job.result = JobStatus.SUCCEEDED, JobResult.SKIPPED_ALREADY_PROCESSED
                job.error_code = job.error_message = None
                job.finished_at = now
                await uow.ingestion_jobs.save(job)
                await uow.commit()
                return RunOutcome.SUCCEEDED
            if not self._ready_for_phase(version):
                return RunOutcome.SKIPPED
            if job.attempt == 0 or job.error_code is not None or job.status is JobStatus.RUNNING:
                job.attempt += 1
            job.status, job.stage = JobStatus.RUNNING, JobStage.AUTHORIZE
            job.started_at = job.started_at or now
            job.error_code = job.error_message = None
            await uow.ingestion_jobs.save(job)
            await uow.documents.set_status(document.id, DocumentStatus.PROCESSING, at=now)
            await uow.commit()
        return Work(job, document, version)

    async def _linked_knowledge_bases(self, work: Work) -> list[KnowledgeBase]:
        """Stage 2: the document and its source are live; the knowledge bases that include the source."""
        async with self._uow() as uow:
            document = await uow.documents.get(work.document.id)
            if document is None or document.deleted_at is not None:
                return []
            source = await uow.data_sources.get(document.data_source_id)
            if (
                source is None
                or source.deleted_at is not None
                or source.status is not DataSourceStatus.ACTIVE
            ):
                return []
            return await uow.knowledge_bases.linked_to_source(source.id)

    async def _set_document_status(self, work: Work, status: DocumentStatus) -> None:
        async with self._uow() as uow:
            await uow.documents.set_status(work.document.id, status, at=self._clock.now())
            await uow.commit()

    async def _set_stage(self, work: Work, stage: JobStage) -> None:
        async with self._uow() as uow:
            job = await uow.ingestion_jobs.get_for_update(work.job.id)
            if job is None:
                raise TransientIngestionError("job disappeared")
            job.stage = stage
            await uow.ingestion_jobs.save(job)
            await uow.commit()
        work.job.stage = stage

    async def _fail_permanently(self, work: Work, code: str, message: str) -> None:
        now = self._clock.now()
        async with self._uow() as uow:
            await uow.documents.fail_version(work.version.id, error_code=code, error_message=message, at=now)
            await uow.commit()
        await self._set_document_status(work, DocumentStatus.FAILED)
        # A rejected file is not kept: quarantine holds only files that may still be processed.
        await self._storage.delete(StorageArea.QUARANTINE, StorageKey(work.version.storage_key))
        await self._finish(work, JobStatus.FAILED, error_code=code, error_message=message)

    async def _transient(self, work: Work) -> RunOutcome:
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
        work: Work,
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


class RunIngestionJob(JobRunner):
    """Extract phase: stages 2-7, then the job is handed to the ``embeddings`` queue."""

    phase = Phase.EXTRACT

    def __init__(
        self,
        uow: UnitOfWorkFactory,
        storage: StorageProvider,
        clock: Clock,
        parsers: ParserRegistry,
        jobs: JobQueue,
        *,
        archive_limits: ArchiveLimits | None = None,
        parse_limits: ParseLimits | None = None,
    ) -> None:
        super().__init__(uow, storage, clock)
        self._parsers, self._jobs = parsers, jobs
        self._limits = archive_limits or ArchiveLimits()
        self._parse_limits = parse_limits or ParseLimits()

    async def _run(self, work: Work) -> RunOutcome:
        file_format = await self._inspect(work)
        await self._store_original(work, file_format)
        await self._parse(work, file_format)
        await self._hand_off(work)
        return RunOutcome.HANDED_OFF

    async def _inspect(self, work: Work) -> DocumentFormat:
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

    async def _store_original(self, work: Work, file_format: DocumentFormat) -> None:
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

    async def _parse(self, work: Work, file_format: DocumentFormat) -> None:
        """Stages 6-7: text and structure, kept as JSON next to the original (reindex does not re-parse)."""
        version = work.version
        if version.extracted_storage_key is not None and await self._storage.exists(
            StorageArea.DOCUMENTS, StorageKey(version.extracted_storage_key)
        ):
            return  # parsed by an earlier attempt
        await self._set_stage(work, JobStage.PARSE)
        parser = self._parsers.for_format(file_format)
        async with self._storage.local_path(StorageArea.DOCUMENTS, StorageKey(version.storage_key)) as path:
            document = await parser.parse(path, file_format, self._parse_limits)
        await self._set_stage(work, JobStage.NORMALIZE)
        payload = json.dumps(document.to_json(), ensure_ascii=False, separators=(",", ":")).encode()
        key = StorageKey.new()
        await self._storage.put(StorageArea.DOCUMENTS, key, _single(payload))
        try:
            async with self._uow() as uow:
                await uow.documents.record_extraction(
                    version.id,
                    extracted_storage_key=key.value,
                    parser=parser.name,
                    page_count=document.page_count,
                    at=self._clock.now(),
                )
                await uow.commit()
        except BaseException:
            await self._storage.delete(StorageArea.DOCUMENTS, key)
            raise
        version.extracted_storage_key = key.value

    async def _hand_off(self, work: Work) -> None:
        """The job waits for the index phase: ``queued`` at stage ``chunk``, without an error."""
        async with self._uow() as uow:
            job = await uow.ingestion_jobs.get_for_update(work.job.id)
            if job is None:
                raise TransientIngestionError("job disappeared")
            job.status, job.stage = JobStatus.QUEUED, JobStage.CHUNK
            job.error_code = job.error_message = None
            await uow.ingestion_jobs.save(job)
            await uow.commit()
        work.job.stage = JobStage.CHUNK
        task_id = await self._jobs.enqueue_indexing(work.job.id)
        async with self._uow() as uow:
            await uow.ingestion_jobs.set_task_id(work.job.id, task_id)
            await uow.commit()


async def _single(payload: bytes) -> AsyncIterator[bytes]:
    yield payload


_OUTCOMES: Final = {
    JobStatus.SUCCEEDED: RunOutcome.SUCCEEDED,
    JobStatus.FAILED: RunOutcome.FAILED,
    JobStatus.CANCELLED: RunOutcome.CANCELLED,
    JobStatus.DEAD: RunOutcome.DEAD,
}
