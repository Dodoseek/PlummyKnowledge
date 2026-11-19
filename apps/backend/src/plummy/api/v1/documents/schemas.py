"""DTOs for ``/api/v1/documents`` (API_CONTRACTS.md §2.11, §4)."""

from __future__ import annotations

from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from plummy.application.documents.upload import UploadResult
from plummy.domain.documents import DocumentStatus, JobKind, JobStatus


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
