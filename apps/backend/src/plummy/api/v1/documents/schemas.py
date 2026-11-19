"""DTOs for ``/api/v1/documents`` (API_CONTRACTS.md §2.11, §4)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from plummy.api.v1.jobs.schemas import JobResponse
from plummy.application.documents.upload import UploadResult
from plummy.application.documents.use_cases import DocumentView
from plummy.application.pagination import Page
from plummy.domain.documents import DocumentStatus, DocumentVersion, JobKind, JobStatus, VersionStatus
from plummy.domain.knowledge import DataSourceType


class UploadedDocument(BaseModel):
    id: UUID
    filename: str
    processing_status: DocumentStatus


class QueuedJob(BaseModel):
    id: UUID
    type: Literal["ingestion"] = "ingestion"
    kind: JobKind
    status: JobStatus


class UploadResponse(BaseModel):
    document: UploadedDocument
    job: QueuedJob

    @classmethod
    def build(cls, result: UploadResult) -> UploadResponse:
        document, job = result.document, result.job
        return cls(
            document=UploadedDocument(
                id=document.id, filename=document.filename, processing_status=document.processing_status
            ),
            job=QueuedJob(id=job.id, kind=job.kind, status=job.status),
        )


class DataSourceRef(BaseModel):
    id: UUID
    type: DataSourceType


class VersionResponse(BaseModel):
    id: UUID
    version_number: int
    checksum_sha256: str  # hex
    size_bytes: int
    mime_type: str
    page_count: int | None
    status: VersionStatus
    error_code: str | None
    created_at: datetime
    processed_at: datetime | None

    @classmethod
    def build(cls, version: DocumentVersion) -> VersionResponse:
        return cls(
            id=version.id,
            version_number=version.version_number,
            checksum_sha256=version.checksum_sha256.hex(),
            size_bytes=version.size_bytes,
            mime_type=version.mime_type,
            page_count=version.page_count,
            status=version.status,
            error_code=version.error_code,
            created_at=version.created_at,
            processed_at=version.processed_at,
        )


class DocumentResponse(BaseModel):
    """``current_version`` is the indexed one used by retrieval; ``latest_version`` the newest, which may
    still be processing or have failed (its ``error_code`` says why)."""

    id: UUID
    filename: str
    mime_type: str | None
    data_source: DataSourceRef
    external_id: str
    current_version: VersionResponse | None
    latest_version: VersionResponse | None
    processing_status: DocumentStatus
    source_modified_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def build(cls, view: DocumentView) -> DocumentResponse:
        document = view.document
        return cls(
            id=document.id,
            filename=document.filename,
            mime_type=document.mime_type,
            data_source=DataSourceRef(id=view.source.id, type=view.source.type),
            external_id=document.external_id,
            current_version=VersionResponse.build(view.current_version) if view.current_version else None,
            latest_version=VersionResponse.build(view.latest_version) if view.latest_version else None,
            processing_status=document.processing_status,
            created_at=document.created_at,
            updated_at=document.updated_at,
        )


class DocumentListResponse(BaseModel):
    items: list[DocumentResponse]
    next_cursor: str | None
    has_more: bool

    @classmethod
    def from_page(cls, page: Page[DocumentView]) -> DocumentListResponse:
        return cls(
            items=[DocumentResponse.build(view) for view in page.items],
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )


class VersionListResponse(BaseModel):
    items: list[VersionResponse]


class ReindexResponse(BaseModel):
    """``created`` is ``false`` when a queued or running job of the document is returned instead."""

    job: JobResponse
    created: bool
