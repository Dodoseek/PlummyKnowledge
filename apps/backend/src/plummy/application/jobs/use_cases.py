"""Background jobs (API_CONTRACTS.md §2.15): ``jobs.read`` to see them, ``jobs.manage`` to retry or
cancel them.

Ingestion jobs belong to a document, sync jobs to an external source (ADR-020 §8); both are read and
cancelled here, only ingestion jobs are retried (a sync runs again on its schedule or by
``POST /data-sources/{id}/sync``). Job errors are already redacted when stored: a stable code and a short
message without document content or secrets.

Cancelling marks the job ``cancelled`` with ``cancelled_by_admin``; the running worker stops at its next
guarded write (the pipeline and the sync engine lock the job and check it before every write), a queued
message finds a finished job and does nothing. A cancelled ingestion job never activates its chunk set, so
retrieval keeps the current version. Jobs cancelled by an administrator can be retried; those cancelled by
a deletion cannot.
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

from plummy.application.audit import by_user
from plummy.application.errors import ConflictError, NotFoundError
from plummy.application.ingestion.dispatch import enforce_expensive_limit, publish
from plummy.application.pagination import Page, PageRequest
from plummy.application.ports import Clock, JobQuery, JobQueue, RateLimiter, UnitOfWork, UnitOfWorkFactory
from plummy.application.security import ClientInfo, SecurityContext
from plummy.domain.audit import AuditAction
from plummy.domain.documents import DocumentStatus, IngestionJob, JobStage, JobStatus, VersionStatus
from plummy.domain.identity import Permission
from plummy.domain.sources import SyncJob

JOB_NOT_FOUND: Final = "Job not found."
RETRYABLE: Final = frozenset({JobStatus.FAILED, JobStatus.DEAD})
CANCELLED_BY_ADMIN: Final = "cancelled_by_admin"
CANCELLED_MESSAGE: Final = "Cancelled by an administrator."
NOT_RETRYABLE: Final = "Only failed, dead or administrator-cancelled jobs can be retried."
NOT_CANCELLABLE: Final = "Only queued or running jobs can be cancelled."


class JobNotRetryableError(ConflictError):
    code = "job_not_retryable"


class JobNotCancellableError(ConflictError):
    code = "job_not_cancellable"


def retryable(job: IngestionJob) -> bool:
    return job.status in RETRYABLE or (
        job.status is JobStatus.CANCELLED and job.error_code == CANCELLED_BY_ADMIN
    )


async def _source_filter(
    uow: UnitOfWork, knowledge_base_id: UUID | None, data_source_id: UUID | None
) -> tuple[UUID, ...] | None:
    """The sources the filters allow; ``None`` without filters, ``()`` when nothing can match."""
    source_ids: tuple[UUID, ...] | None = None
    if knowledge_base_id is not None:
        kb = await uow.knowledge_bases.get(knowledge_base_id)
        linked = await uow.data_sources.linked_to(kb.id) if kb is not None else []
        source_ids = tuple(source.id for source in linked)
    if data_source_id is not None:
        source_ids = (
            (data_source_id,)
            if source_ids is None
            else tuple(source for source in source_ids if source == data_source_id)
        )
    return source_ids


class ListJobs:
    def __init__(self, uow: UnitOfWorkFactory) -> None:
        self._uow = uow

    async def execute(
        self,
        context: SecurityContext,
        page: PageRequest,
        *,
        status: JobStatus | None = None,
        knowledge_base_id: UUID | None = None,
        data_source_id: UUID | None = None,
    ) -> Page[IngestionJob]:
        context.require(Permission.JOBS_READ)
        async with self._uow() as uow:
            source_ids = await _source_filter(uow, knowledge_base_id, data_source_id)
            if source_ids == ():
                return Page(items=[], next_cursor=None)
            return await uow.ingestion_jobs.page(JobQuery(status=status, source_ids=source_ids), page)


class ListSyncJobs:
    def __init__(self, uow: UnitOfWorkFactory) -> None:
        self._uow = uow

    async def execute(
        self,
        context: SecurityContext,
        page: PageRequest,
        *,
        status: JobStatus | None = None,
        knowledge_base_id: UUID | None = None,
        data_source_id: UUID | None = None,
    ) -> Page[SyncJob]:
        context.require(Permission.JOBS_READ)
        async with self._uow() as uow:
            source_ids = await _source_filter(uow, knowledge_base_id, data_source_id)
            if source_ids == ():
                return Page(items=[], next_cursor=None)
            return await uow.sync_jobs.page(JobQuery(status=status, source_ids=source_ids), page)


class GetJob:
    def __init__(self, uow: UnitOfWorkFactory) -> None:
        self._uow = uow

    async def execute(self, context: SecurityContext, job_id: UUID) -> IngestionJob:
        context.require(Permission.JOBS_READ)
        async with self._uow() as uow:
            job = await uow.ingestion_jobs.get(job_id)
        if job is None:
            raise NotFoundError(JOB_NOT_FOUND)
        return job


class GetSyncJob:
    def __init__(self, uow: UnitOfWorkFactory) -> None:
        self._uow = uow

    async def execute(self, context: SecurityContext, job_id: UUID) -> SyncJob:
        context.require(Permission.JOBS_READ)
        async with self._uow() as uow:
            job = await uow.sync_jobs.get(job_id)
        if job is None:
            raise NotFoundError(JOB_NOT_FOUND)
        return job


class RetryJob:
    """A ``failed``, ``dead`` or administrator-cancelled job runs again with fresh attempts, from the stored
    text when the original was already extracted. A permanently rejected file fails the same way again."""

    def __init__(self, uow: UnitOfWorkFactory, queue: JobQueue, limiter: RateLimiter, clock: Clock) -> None:
        self._uow, self._queue, self._limiter, self._clock = uow, queue, limiter, clock

    async def execute(self, context: SecurityContext, client: ClientInfo, job_id: UUID) -> IngestionJob:
        context.require(Permission.JOBS_MANAGE)
        async with self._uow() as uow:
            job = await uow.ingestion_jobs.get(job_id)
        if job is None:
            raise NotFoundError(JOB_NOT_FOUND)
        if not retryable(job):
            raise JobNotRetryableError(NOT_RETRYABLE)
        await enforce_expensive_limit(self._limiter)
        now = self._clock.now()
        async with self._uow() as uow:
            # Document row first, then jobs: the same lock order as deletion and the pipeline.
            document = await uow.documents.get_for_update(job.document_id)
            locked = await uow.ingestion_jobs.get_for_update(job_id)
            if locked is None:
                raise NotFoundError(JOB_NOT_FOUND)
            if not retryable(locked):
                raise JobNotRetryableError(NOT_RETRYABLE)
            if document is None or document.deleted_at is not None:
                raise JobNotRetryableError("The document of this job was deleted.")
            if await uow.ingestion_jobs.unfinished_for_document(document.id):
                raise JobNotRetryableError("Another job of this document is queued or running.")
            version = (
                await uow.documents.get_version(locked.document_version_id)
                if locked.document_version_id is not None
                else None
            )
            if version is None or version.status is VersionStatus.DELETED:
                raise JobNotRetryableError("The document version of this job no longer exists.")
            locked.status, locked.result = JobStatus.QUEUED, None
            locked.stage = JobStage.CHUNK if version.extracted_storage_key is not None else JobStage.FETCH
            locked.attempt = 0
            locked.error_code = locked.error_message = None
            locked.started_at = locked.finished_at = None
            await uow.ingestion_jobs.save(locked)
            await uow.ingestion_jobs.set_task_id(locked.id, None)
            if version.status is not VersionStatus.READY:
                await uow.documents.reset_version(version.id, at=now)
                await uow.documents.set_status(document.id, DocumentStatus.PENDING, at=now)
            await uow.audit.record(
                by_user(
                    context,
                    client,
                    AuditAction.JOB_RETRIED,
                    target_type="ingestion_job",
                    target_id=job_id,
                    details={"document_id": str(document.id), "previous_status": str(job.status)},
                )
            )
            await uow.commit()
        await publish(self._uow, self._queue, locked)
        return locked


class CancelJob:
    """A ``queued`` or ``running`` ingestion job stops. The version it was building fails with
    ``cancelled_by_admin`` (unless it is the ready one being reindexed); the document stays ``indexed``
    when it still has a current version, otherwise it is ``failed`` until a retry or reindex."""

    def __init__(self, uow: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def execute(self, context: SecurityContext, client: ClientInfo, job_id: UUID) -> IngestionJob:
        context.require(Permission.JOBS_MANAGE)
        now = self._clock.now()
        async with self._uow() as uow:
            unlocked = await uow.ingestion_jobs.get(job_id)
            if unlocked is None:
                raise NotFoundError(JOB_NOT_FOUND)
            # Document row first, then the job: the lock order of deletion and the pipeline.
            document = await uow.documents.get_for_update(unlocked.document_id)
            job = await uow.ingestion_jobs.get_for_update(job_id)
            if job is None:
                raise NotFoundError(JOB_NOT_FOUND)
            if job.status.finished:
                raise JobNotCancellableError(NOT_CANCELLABLE)
            previous = job.status
            job.status, job.result = JobStatus.CANCELLED, None
            job.error_code, job.error_message, job.finished_at = CANCELLED_BY_ADMIN, CANCELLED_MESSAGE, now
            await uow.ingestion_jobs.save(job)
            if document is not None and document.deleted_at is None:
                version = (
                    await uow.documents.get_version(job.document_version_id)
                    if job.document_version_id is not None
                    else None
                )
                if version is not None and version.status not in (VersionStatus.READY, VersionStatus.DELETED):
                    await uow.documents.fail_version(
                        version.id, error_code=CANCELLED_BY_ADMIN, error_message=CANCELLED_MESSAGE, at=now
                    )
                status = (
                    DocumentStatus.INDEXED
                    if document.current_version_id is not None
                    else DocumentStatus.FAILED
                )
                await uow.documents.set_status(document.id, status, at=now)
            await uow.audit.record(
                by_user(
                    context,
                    client,
                    AuditAction.JOB_CANCELLED,
                    target_type="ingestion_job",
                    target_id=job_id,
                    details={"document_id": str(job.document_id), "previous_status": str(previous)},
                )
            )
            await uow.commit()
        return job


class CancelSyncJob:
    """A ``queued`` or ``running`` sync job stops at its next checkpoint; documents it already handed to
    ingestion keep their own jobs. The next scheduled or manual sync starts from the stored cursor."""

    def __init__(self, uow: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def execute(self, context: SecurityContext, client: ClientInfo, job_id: UUID) -> SyncJob:
        context.require(Permission.JOBS_MANAGE)
        now = self._clock.now()
        async with self._uow() as uow:
            unlocked = await uow.sync_jobs.get(job_id)
            if unlocked is None:
                raise NotFoundError(JOB_NOT_FOUND)
            # Source row first, then the job: the lock order of the sync engine.
            await uow.data_sources.get_for_update(unlocked.data_source_id)
            job = await uow.sync_jobs.get_for_update(job_id)
            if job is None:
                raise NotFoundError(JOB_NOT_FOUND)
            if job.status.finished:
                raise JobNotCancellableError(NOT_CANCELLABLE)
            previous = job.status
            job.status, job.error_code, job.error_message = (
                JobStatus.CANCELLED,
                CANCELLED_BY_ADMIN,
                CANCELLED_MESSAGE,
            )
            job.finished_at = job.updated_at = now
            await uow.sync_jobs.save(job)
            await uow.audit.record(
                by_user(
                    context,
                    client,
                    AuditAction.JOB_CANCELLED,
                    target_type="sync_job",
                    target_id=job_id,
                    details={"data_source_id": str(job.data_source_id), "previous_status": str(previous)},
                )
            )
            await uow.commit()
        return job
