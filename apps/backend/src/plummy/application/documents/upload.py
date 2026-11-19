"""Upload intake: stage 1 of the pipeline for the ``upload`` source (ADR-008 §3, API_CONTRACTS.md §2.11).

The request only puts the file into quarantine and queues a job; all processing runs in the worker. Order of
checks: KB ``manage`` access and the rate limit before a single byte is stored; the size limit while the file
streams; a first look at the type from its first bytes. The worker checks the whole file again (stages 2-4).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from plummy.application.audit import by_user
from plummy.application.auth.throttle import Limit
from plummy.application.errors import (
    ConflictError,
    InvalidInputError,
    PayloadTooLargeError,
    RateLimitedError,
    ServiceUnavailableError,
    UnsupportedMediaTypeError,
)
from plummy.application.ingestion.dispatch import publish
from plummy.application.knowledge.access import require_kb_access
from plummy.application.ports import (
    Clock,
    JobQueue,
    RateLimiter,
    RateLimiterUnavailableError,
    UnitOfWork,
    UnitOfWorkFactory,
)
from plummy.application.security import ClientInfo, SecurityContext
from plummy.application.storage import (
    StorageArea,
    StorageKey,
    StorageLimitExceededError,
    StorageProvider,
    StoredObject,
)
from plummy.core.ids import new_id
from plummy.domain.audit import AuditAction
from plummy.domain.documents import (
    Document,
    DocumentStatus,
    DocumentVersion,
    IngestionJob,
    JobKind,
    JobStage,
    JobStatus,
    VersionStatus,
    display_filename,
    ingest_idempotency_key,
    upload_external_id,
)
from plummy.domain.file_types import SNIFF_BYTES, provisional_format, sniff_container
from plummy.domain.knowledge import DataSourceStatus, KbAccessLevel, KnowledgeBase

UPLOADS_PER_MINUTE: Final = 30
MAX_ATTEMPTS: Final = 5


@dataclass(frozen=True, slots=True)
class UploadResult:
    document: Document
    job: IngestionJob


class UploadSourceUnavailableError(ConflictError):
    """The upload source of the knowledge base is missing or not active."""

    code = "source_unavailable"


class UploadDocument:
    def __init__(
        self,
        uow: UnitOfWorkFactory,
        storage: StorageProvider,
        queue: JobQueue,
        limiter: RateLimiter,
        clock: Clock,
        *,
        max_bytes: int,
    ) -> None:
        self._uow, self._storage, self._queue = uow, storage, queue
        self._limiter, self._clock, self._max_bytes = limiter, clock, max_bytes

    async def execute(
        self,
        context: SecurityContext,
        client: ClientInfo,
        kb_id: UUID,
        filename: str | None,
        content: AsyncIterator[bytes],
    ) -> UploadResult:
        async with self._uow() as uow:
            await self._authorize(uow, context, kb_id)
        await self._enforce_rate_limit(context)

        key = StorageKey.new()
        try:
            stored = await self._storage.put(StorageArea.QUARANTINE, key, content, max_bytes=self._max_bytes)
        except StorageLimitExceededError:
            raise PayloadTooLargeError(f"The file is larger than {self._max_bytes} bytes.") from None
        try:
            result = await self._register(context, client, kb_id, key, stored, display_filename(filename))
        except BaseException:
            await self._storage.delete(StorageArea.QUARANTINE, key)
            raise
        await self._enqueue(result.job)
        return result

    async def _authorize(
        self, uow: UnitOfWork, context: SecurityContext, kb_id: UUID
    ) -> tuple[KnowledgeBase, UUID]:
        kb, _ = await require_kb_access(uow, context, kb_id, KbAccessLevel.MANAGE)
        source = await uow.data_sources.upload_source_of(kb.id)
        if source is None or source.status is not DataSourceStatus.ACTIVE:
            raise UploadSourceUnavailableError("Uploads to this knowledge base are not possible right now.")
        return kb, source.id

    async def _enforce_rate_limit(self, context: SecurityContext) -> None:
        limit = Limit(f"upload:user:{context.user_id.hex}:1m", UPLOADS_PER_MINUTE, 60)
        try:
            status = await self._limiter.hit(
                limit.name, limit=limit.limit, window_seconds=limit.window_seconds
            )
        except RateLimiterUnavailableError:
            raise ServiceUnavailableError() from None
        if not status.allowed:
            raise RateLimitedError(status.retry_after_seconds)

    async def _head(self, key: StorageKey) -> bytes:
        head = bytearray()
        async for chunk in self._storage.open(StorageArea.QUARANTINE, key):
            head += chunk
            if len(head) >= SNIFF_BYTES:
                break
        return bytes(head[:SNIFF_BYTES])

    async def _register(
        self,
        context: SecurityContext,
        client: ClientInfo,
        kb_id: UUID,
        key: StorageKey,
        stored: StoredObject,
        filename: str,
    ) -> UploadResult:
        if stored.size_bytes == 0:
            raise InvalidInputError("file", "empty_file", "The file is empty.")
        head = await self._head(key)
        kind = sniff_container(head, complete=stored.size_bytes <= len(head))
        if kind is None:
            raise UnsupportedMediaTypeError(
                "Supported formats: PDF, DOCX, XLSX, TXT, CSV, Markdown (decided by the file content)."
            )
        file_format = provisional_format(kind, filename)
        now = self._clock.now()
        async with self._uow() as uow:
            # Access is checked again: it may have been revoked while the file was streaming.
            kb, source_id = await self._authorize(uow, context, kb_id)
            document_id, version_id = new_id(), new_id()
            document = Document(
                id=document_id,
                data_source_id=source_id,
                external_id=upload_external_id(document_id),
                filename=filename,
                mime_type=file_format.mime_type,
                processing_status=DocumentStatus.PENDING,
                current_version_id=None,
                created_at=now,
                updated_at=now,
            )
            version = DocumentVersion(
                id=version_id,
                document_id=document_id,
                version_number=1,
                checksum_sha256=stored.sha256,
                mime_type=file_format.mime_type,
                size_bytes=stored.size_bytes,
                storage_key=key.value,
                status=VersionStatus.PENDING,
                created_at=now,
                updated_at=now,
            )
            job = IngestionJob(
                id=new_id(),
                document_id=document_id,
                document_version_id=version_id,
                kind=JobKind.INGEST,
                status=JobStatus.QUEUED,
                stage=JobStage.FETCH,
                idempotency_key=ingest_idempotency_key(version_id, kb.embedding_profile_id, {}),
                attempt=0,
                max_attempts=MAX_ATTEMPTS,
                requested_by=context.user_id,
                queued_at=now,
            )
            await uow.documents.add(document)
            await uow.documents.add_version(version)
            await uow.ingestion_jobs.add(job)
            await uow.audit.record(
                by_user(
                    context,
                    client,
                    AuditAction.DOCUMENT_UPLOADED,
                    target_type="document",
                    target_id=document_id,
                    details={
                        "knowledge_base_id": str(kb.id),
                        "job_id": str(job.id),
                        "size_bytes": stored.size_bytes,
                        "format": str(file_format),
                    },
                )
            )
            await uow.commit()
        return UploadResult(document, job)

    async def _enqueue(self, job: IngestionJob) -> None:
        """The job is durable in PostgreSQL first; a broker outage leaves it ``queued`` for the next sweep."""
        await publish(self._uow, self._queue, job)
