"""Documents (API_CONTRACTS.md §2.11, ADR-008, DATABASE_SCHEMA.md §8).

A document is visible through the knowledge bases that include its source: ``read`` on any of them lets the
caller see it, ``manage`` lets them delete or reindex it. Without access the answer is ``404``, as for a
document that does not exist; deleted documents are never shown.

Deletion is a soft delete in one transaction (the document leaves retrieval and listings at once, its jobs
are cancelled) followed by an immediate purge of chunks, vectors and files on the ``maintenance`` queue.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from plummy.application.audit import by_user
from plummy.application.errors import ConflictError, NotFoundError, PermissionDeniedError
from plummy.application.ingestion.dispatch import enforce_expensive_limit, publish
from plummy.application.knowledge.access import effective_access, require_kb_access
from plummy.application.pagination import Page, PageRequest
from plummy.application.ports import (
    Clock,
    DocumentQuery,
    JobQueue,
    JobQueueUnavailableError,
    RateLimiter,
    UnitOfWork,
    UnitOfWorkFactory,
)
from plummy.application.security import ClientInfo, SecurityContext
from plummy.core.ids import new_id
from plummy.domain.audit import AuditAction
from plummy.domain.documents import (
    DeletionReason,
    Document,
    DocumentStatus,
    DocumentVersion,
    IngestionJob,
    JobKind,
    JobStage,
    JobStatus,
    VersionStatus,
)
from plummy.domain.knowledge import DataSource, KbAccessLevel, strongest

logger = logging.getLogger(__name__)

DOCUMENT_NOT_FOUND: Final = "Document not found."
DOCUMENT_MANAGE_DENIED: Final = "You do not have permission to manage this document."
MAX_ATTEMPTS: Final = 5
CANCELLED_BY_DELETION: Final = "document_deleted"


@dataclass(frozen=True, slots=True)
class DocumentView:
    document: Document
    source: DataSource
    current_version: DocumentVersion | None
    latest_version: DocumentVersion | None


@dataclass(frozen=True, slots=True)
class ReindexResult:
    document: Document
    job: IngestionJob
    created: bool  # ``False``: a queued or running job of the document is returned instead


class NoVersionError(ConflictError):
    code = "no_version"


async def require_document_access(
    uow: UnitOfWork, context: SecurityContext, document: Document | None, required: KbAccessLevel
) -> tuple[Document, DataSource]:
    """The live document and its source; ``404`` without access, ``403`` with ``read`` where ``manage`` is
    needed."""
    if document is None or document.deleted_at is not None:
        raise NotFoundError(DOCUMENT_NOT_FOUND)
    source = await uow.data_sources.get(document.data_source_id)
    if source is None or source.deleted_at is not None:
        raise NotFoundError(DOCUMENT_NOT_FOUND)
    kb_ids = [kb.id for kb in await uow.knowledge_bases.linked_to_source(source.id)]
    level = strongest(list((await effective_access(uow, context, kb_ids)).values()))
    if level is None:
        raise NotFoundError(DOCUMENT_NOT_FOUND)
    if not level.allows(required):
        raise PermissionDeniedError(DOCUMENT_MANAGE_DENIED)
    return document, source


async def _views(
    uow: UnitOfWork, documents: list[Document], sources: dict[UUID, DataSource]
) -> list[DocumentView]:
    latest = await uow.documents.latest_versions([document.id for document in documents])
    views: list[DocumentView] = []
    for document in documents:
        newest = latest.get(document.id)
        current: DocumentVersion | None = None
        if document.current_version_id is not None:
            current = (
                newest
                if newest is not None and newest.id == document.current_version_id
                else await uow.documents.get_version(document.current_version_id)
            )
        views.append(DocumentView(document, sources[document.data_source_id], current, newest))
    return views


class ListDocuments:
    """Documents of one knowledge base; ``data_source_id`` may narrow it to one of its sources."""

    def __init__(self, uow: UnitOfWorkFactory) -> None:
        self._uow = uow

    async def execute(
        self,
        context: SecurityContext,
        kb_id: UUID,
        page: PageRequest,
        *,
        status: DocumentStatus | None = None,
        data_source_id: UUID | None = None,
        search: str | None = None,
    ) -> Page[DocumentView]:
        async with self._uow() as uow:
            kb, _ = await require_kb_access(uow, context, kb_id, KbAccessLevel.READ)
            sources = {
                source.id: source
                for source in await uow.data_sources.linked_to(kb.id)
                if data_source_id is None or source.id == data_source_id
            }
            if not sources:
                return Page(items=[], next_cursor=None)
            result = await uow.documents.page(
                DocumentQuery(source_ids=tuple(sources), status=status, search=search), page
            )
            views = await _views(uow, result.items, sources)
        return Page(items=views, next_cursor=result.next_cursor)


class GetDocument:
    def __init__(self, uow: UnitOfWorkFactory) -> None:
        self._uow = uow

    async def execute(self, context: SecurityContext, document_id: UUID) -> DocumentView:
        async with self._uow() as uow:
            document, source = await require_document_access(
                uow, context, await uow.documents.get(document_id), KbAccessLevel.READ
            )
            return (await _views(uow, [document], {source.id: source}))[0]


class ListDocumentVersions:
    def __init__(self, uow: UnitOfWorkFactory) -> None:
        self._uow = uow

    async def execute(self, context: SecurityContext, document_id: UUID) -> list[DocumentVersion]:
        async with self._uow() as uow:
            await require_document_access(
                uow, context, await uow.documents.get(document_id), KbAccessLevel.READ
            )
            return await uow.documents.versions(document_id)


class DeleteDocument:
    def __init__(self, uow: UnitOfWorkFactory, queue: JobQueue, clock: Clock) -> None:
        self._uow, self._queue, self._clock = uow, queue, clock

    async def execute(self, context: SecurityContext, client: ClientInfo, document_id: UUID) -> None:
        now = self._clock.now()
        async with self._uow() as uow:
            # Document row first, then its jobs: the same lock order as the pipeline.
            await require_document_access(
                uow, context, await uow.documents.get_for_update(document_id), KbAccessLevel.MANAGE
            )
            cancelled = 0
            for job in await uow.ingestion_jobs.unfinished_for_document(document_id):
                job.status, job.error_code, job.error_message = (
                    JobStatus.CANCELLED,
                    CANCELLED_BY_DELETION,
                    None,
                )
                job.finished_at = now
                await uow.ingestion_jobs.save(job)
                cancelled += 1
            await uow.documents.mark_deleted(document_id, DeletionReason.USER, at=now)
            await uow.audit.record(
                by_user(
                    context,
                    client,
                    AuditAction.DOCUMENT_DELETED,
                    target_type="document",
                    target_id=document_id,
                    details={"reason": str(DeletionReason.USER), "jobs_cancelled": cancelled},
                )
            )
            await uow.commit()
        try:
            await self._queue.enqueue_purge(document_id)
        except JobQueueUnavailableError:
            # Already out of retrieval; the periodic sweep purges it later.
            logger.warning("purge not published, broker unavailable", extra={"document_id": str(document_id)})


class ReindexDocument:
    """Runs the pipeline again for the document's version. An indexed version with the same settings ends as
    ``skipped_already_processed`` (ADR-008 §4); a failed one is processed again from the stored text when
    it was extracted, else from the original."""

    def __init__(self, uow: UnitOfWorkFactory, queue: JobQueue, limiter: RateLimiter, clock: Clock) -> None:
        self._uow, self._queue, self._limiter, self._clock = uow, queue, limiter, clock

    async def execute(self, context: SecurityContext, client: ClientInfo, document_id: UUID) -> ReindexResult:
        async with self._uow() as uow:
            await require_document_access(
                uow, context, await uow.documents.get(document_id), KbAccessLevel.MANAGE
            )
        await enforce_expensive_limit(self._limiter)
        now = self._clock.now()
        async with self._uow() as uow:
            document, _ = await require_document_access(
                uow, context, await uow.documents.get_for_update(document_id), KbAccessLevel.MANAGE
            )
            pending = await uow.ingestion_jobs.unfinished_for_document(document_id)
            if pending:
                job, created = pending[0], False
            else:
                version = await self._target_version(uow, document)
                job, created = self._new_job(context, document, version, now), True
                await uow.ingestion_jobs.add(job)
                if version.status is not VersionStatus.READY:
                    await uow.documents.reset_version(version.id, at=now)
                    await uow.documents.set_status(document.id, DocumentStatus.PENDING, at=now)
            await uow.audit.record(
                by_user(
                    context,
                    client,
                    AuditAction.DOCUMENT_REINDEX_REQUESTED,
                    target_type="document",
                    target_id=document_id,
                    details={"job_id": str(job.id), "new_job": created},
                )
            )
            await uow.commit()
        if created:
            await publish(self._uow, self._queue, job)
        return ReindexResult(document, job, created)

    @staticmethod
    async def _target_version(uow: UnitOfWork, document: Document) -> DocumentVersion:
        version = None
        if document.current_version_id is not None:
            version = await uow.documents.get_version(document.current_version_id)
        if version is None:
            versions = await uow.documents.versions(document.id)
            version = versions[0] if versions else None
        if version is None or version.status is VersionStatus.DELETED:
            raise NoVersionError("The document has no version to index.")
        return version

    @staticmethod
    def _new_job(
        context: SecurityContext, document: Document, version: DocumentVersion, now: datetime
    ) -> IngestionJob:
        job_id = new_id()
        return IngestionJob(
            id=job_id,
            document_id=document.id,
            document_version_id=version.id,
            kind=JobKind.REINDEX,
            status=JobStatus.QUEUED,
            # The stored text is reused: the index phase only (no parsing in the network-free worker again).
            stage=JobStage.CHUNK if version.extracted_storage_key is not None else JobStage.FETCH,
            idempotency_key=f"reindex:{version.id.hex}:{job_id.hex}",
            attempt=0,
            max_attempts=MAX_ATTEMPTS,
            requested_by=context.user_id,
            queued_at=now,
        )
