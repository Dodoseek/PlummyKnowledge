"""Background jobs (API_CONTRACTS.md §2.15): ``jobs.read`` to see them, ``jobs.manage`` to retry.

Ingestion jobs belong to a document, sync jobs to an external source (ADR-020 §8); both are read here, only
ingestion jobs are retried (a sync runs again on its schedule or by ``POST /data-sources/{id}/sync``). Job
errors are already redacted when stored: a stable code and a short message without document content or
secrets.
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


class JobNotRetryableError(ConflictError):
    code = "job_not_retryable"


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
    """A ``failed`` or ``dead`` job runs again with fresh attempts, from the stored text when the original
    was already extracted. A permanently rejected file fails the same way again."""

    def __init__(self, uow: UnitOfWorkFactory, queue: JobQueue, limiter: RateLimiter, clock: Clock) -> None:
        self._uow, self._queue, self._limiter, self._clock = uow, queue, limiter, clock

    async def execute(self, context: SecurityContext, client: ClientInfo, job_id: UUID) -> IngestionJob:
        context.require(Permission.JOBS_MANAGE)
        async with self._uow() as uow:
            job = await uow.ingestion_jobs.get(job_id)
        if job is None:
            raise NotFoundError(JOB_NOT_FOUND)
        if job.status not in RETRYABLE:
            raise JobNotRetryableError("Only failed or dead jobs can be retried.")
        await enforce_expensive_limit(self._limiter)
        now = self._clock.now()
        async with self._uow() as uow:
            # Document row first, then jobs: the same lock order as deletion and the pipeline.
            document = await uow.documents.get_for_update(job.document_id)
            locked = await uow.ingestion_jobs.get_for_update(job_id)
            if locked is None:
                raise NotFoundError(JOB_NOT_FOUND)
            if locked.status not in RETRYABLE:
                raise JobNotRetryableError("Only failed or dead jobs can be retried.")
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
