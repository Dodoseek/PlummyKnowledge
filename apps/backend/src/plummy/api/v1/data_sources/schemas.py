"""DTOs of ``/api/v1/data-sources`` (API_CONTRACTS.md §2.9, ADR-020). Secrets are never part of a response:
credentials show only the account, the granted scopes and their state."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from plummy.application.pagination import Page
from plummy.application.sources.ports import BrowseKind, BrowsePage
from plummy.application.sources.use_cases import DataSourceView, ValidationReport, ValidationStatus
from plummy.domain.documents import JobStatus
from plummy.domain.knowledge import DataSourceStatus, DataSourceType
from plummy.domain.sources import CredentialStatus, SyncJob, SyncMode, SyncTrigger

MAX_NAME_INPUT_LENGTH = 400


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class FolderResponse(BaseModel):
    id: str
    name: str | None


class SyncStateResponse(BaseModel):
    interval_minutes: int | None
    next_sync_at: datetime | None
    last_sync_at: datetime | None
    last_reconciled_at: datetime | None
    last_status: str | None
    last_error: str | None


class CredentialsResponse(BaseModel):
    status: CredentialStatus
    account_label: str | None
    granted_scopes: list[str]
    connected_at: datetime
    last_refreshed_at: datetime | None


class DataSourceResponse(BaseModel):
    id: UUID
    type: DataSourceType
    name: str
    status: DataSourceStatus
    knowledge_base_id: UUID
    owner_knowledge_base_id: UUID | None
    folder: FolderResponse | None
    sync: SyncStateResponse | None
    credentials: CredentialsResponse | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def build(cls, view: DataSourceView) -> DataSourceResponse:
        source, credentials = view.source, view.credentials
        folder_id = source.config.get("folder_id")
        return cls(
            id=source.id,
            type=source.type,
            name=source.name,
            status=source.status,
            knowledge_base_id=view.knowledge_base_id,
            owner_knowledge_base_id=source.owner_knowledge_base_id,
            folder=FolderResponse(id=folder_id, name=source.config.get("folder_name")) if folder_id else None,
            sync=SyncStateResponse(
                interval_minutes=source.sync_interval_minutes,
                next_sync_at=source.next_sync_at,
                last_sync_at=source.last_sync_at,
                last_reconciled_at=source.last_reconciled_at,
                last_status=str(source.last_sync_status) if source.last_sync_status else None,
                last_error=source.last_error,
            )
            if source.is_external
            else None,
            credentials=CredentialsResponse(
                status=credentials.status,
                account_label=credentials.account_label,
                granted_scopes=list(credentials.granted_scopes),
                connected_at=credentials.created_at,
                last_refreshed_at=credentials.last_refreshed_at,
            )
            if credentials is not None
            else None,
            created_at=source.created_at,
            updated_at=source.updated_at,
        )


class DataSourceListResponse(BaseModel):
    items: list[DataSourceResponse]
    next_cursor: str | None
    has_more: bool

    @classmethod
    def from_page(cls, page: Page[DataSourceView]) -> DataSourceListResponse:
        return cls(
            items=[DataSourceResponse.build(item) for item in page.items],
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )


class CreateDataSourceRequest(_Request):
    type: DataSourceType
    name: str = Field(min_length=1, max_length=MAX_NAME_INPUT_LENGTH)
    knowledge_base_id: UUID
    sync_interval_minutes: int | None = 15


class UpdateDataSourceRequest(_Request):
    name: str | None = Field(default=None, min_length=1, max_length=MAX_NAME_INPUT_LENGTH)
    sync_interval_minutes: int | None = None
    folder_id: str | None = Field(default=None, max_length=256)  # ``null`` removes the folder

    def patch_values(self) -> dict[str, Any]:
        """Only the fields that were sent: ``sync_interval_minutes: null`` means manual sync only,
        ``folder_id: null`` removes the folder."""
        return {name: getattr(self, name) for name in self.model_fields_set}


class OAuthStartResponse(BaseModel):
    authorization_url: str


class DeletionResponse(BaseModel):
    documents_deleted: int


class BrowseEntryResponse(BaseModel):
    id: str
    name: str
    kind: BrowseKind


class BrowseResponse(BaseModel):
    items: list[BrowseEntryResponse]
    next_cursor: str | None
    has_more: bool

    @classmethod
    def build(cls, page: BrowsePage) -> BrowseResponse:
        return cls(
            items=[
                BrowseEntryResponse(id=entry.id, name=entry.name, kind=entry.kind) for entry in page.entries
            ],
            next_cursor=page.next_page_token,
            has_more=page.next_page_token is not None,
        )


class ValidationResponse(BaseModel):
    status: ValidationStatus
    checked_at: datetime
    error_code: str | None
    account_label: str | None
    folder_accessible: bool | None

    @classmethod
    def build(cls, report: ValidationReport) -> ValidationResponse:
        return cls(
            status=report.status,
            checked_at=report.checked_at,
            error_code=report.error_code,
            account_label=report.account_label,
            folder_accessible=report.folder_accessible,
        )


class SyncRequest(_Request):
    mode: Literal["incremental", "reconciliation"] = "incremental"

    def sync_mode(self) -> SyncMode:
        return SyncMode(self.mode)


class SyncErrorResponse(BaseModel):
    code: str


class SyncJobResponse(BaseModel):
    """A sync job; ``mode`` is ``initial`` when the source had no cursor yet (a full listing)."""

    id: UUID
    type: Literal["sync"] = "sync"
    data_source_id: UUID
    trigger: SyncTrigger
    mode: SyncMode
    status: JobStatus
    stats: dict[str, int]
    attempt: int
    max_attempts: int
    error: SyncErrorResponse | None
    queued_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def build(cls, job: SyncJob) -> SyncJobResponse:
        return cls(
            id=job.id,
            data_source_id=job.data_source_id,
            trigger=job.trigger,
            mode=job.mode,
            status=job.status,
            stats=job.stats.as_dict(),
            attempt=job.attempt,
            max_attempts=job.max_attempts,
            error=SyncErrorResponse(code=job.error_code) if job.error_code else None,
            queued_at=job.queued_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
        )
