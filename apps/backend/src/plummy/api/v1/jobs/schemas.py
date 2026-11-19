"""DTOs for ``/api/v1/jobs`` (API_CONTRACTS.md §2.15, §4 ``Job``)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

from plummy.application.pagination import Page
from plummy.domain.documents import IngestionJob, JobKind, JobResult, JobStage, JobStatus


class JobError(BaseModel):
    """Stored already redacted: a stable code and a short message without document content or secrets."""

    code: str
    message: str | None


class JobResponse(BaseModel):
    id: UUID
    type: Literal["ingestion"] = "ingestion"
    kind: JobKind
    status: JobStatus
    stage: JobStage
    result: JobResult | None
    attempt: int
    max_attempts: int
    error: JobError | None
    document_id: UUID
    document_version_id: UUID | None
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def build(cls, job: IngestionJob) -> JobResponse:
        return cls(
            id=job.id,
            kind=job.kind,
            status=job.status,
            stage=job.stage,
            result=job.result,
            attempt=job.attempt,
            max_attempts=job.max_attempts,
            error=JobError(code=job.error_code, message=job.error_message) if job.error_code else None,
            document_id=job.document_id,
            document_version_id=job.document_version_id,
            queued_at=job.queued_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
        )


class JobListResponse(BaseModel):
    items: list[JobResponse]
    next_cursor: str | None
    has_more: bool

    @classmethod
    def from_page(cls, page: Page[IngestionJob]) -> JobListResponse:
        return cls(
            items=[JobResponse.build(job) for job in page.items],
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )
