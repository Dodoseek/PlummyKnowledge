"""Credentials and sync jobs of external sources on PostgreSQL (DATABASE_SCHEMA.md §5.5, §7.2, ADR-020)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from plummy.application.pagination import Page, PageRequest
from plummy.application.ports import JobQuery, UniqueViolationError
from plummy.domain.documents import JobStatus
from plummy.domain.sources import (
    CredentialStatus,
    SourceCredentials,
    SyncJob,
    SyncMode,
    SyncStats,
    SyncTrigger,
)
from plummy.infrastructure.db.models import DataSourceCredentialModel, SyncJobModel
from plummy.infrastructure.db.paging import paginate

_ACTIVE = (str(JobStatus.QUEUED), str(JobStatus.RUNNING))
ACTIVE_JOB_INDEX = "sync_jobs_one_active_per_source"


def _credentials(row: DataSourceCredentialModel) -> SourceCredentials:
    return SourceCredentials(
        data_source_id=row.data_source_id,
        secret_ref_id=row.secret_ref_id,
        account_label=row.account_label,
        granted_scopes=tuple(row.granted_scopes),
        status=CredentialStatus(row.status),
        created_at=row.created_at,
        updated_at=row.updated_at,
        connected_by=row.connected_by,
        last_refreshed_at=row.last_refreshed_at,
        revoked_at=row.revoked_at,
    )


class SqlSourceCredentialRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session

    async def get(self, source_id: UUID) -> SourceCredentials | None:
        row = await self._db.get(DataSourceCredentialModel, source_id, populate_existing=True)
        return _credentials(row) if row is not None else None

    async def add(self, credentials: SourceCredentials) -> None:
        self._db.add(
            DataSourceCredentialModel(
                data_source_id=credentials.data_source_id,
                secret_ref_id=credentials.secret_ref_id,
                account_label=credentials.account_label,
                granted_scopes=list(credentials.granted_scopes),
                status=str(credentials.status),
                created_at=credentials.created_at,
                updated_at=credentials.updated_at,
                connected_by=credentials.connected_by,
                last_refreshed_at=credentials.last_refreshed_at,
                revoked_at=credentials.revoked_at,
            )
        )
        await self._db.flush()

    async def save(self, credentials: SourceCredentials) -> None:
        await self._db.execute(
            update(DataSourceCredentialModel)
            .where(DataSourceCredentialModel.data_source_id == credentials.data_source_id)
            .values(
                secret_ref_id=credentials.secret_ref_id,
                account_label=credentials.account_label,
                granted_scopes=list(credentials.granted_scopes),
                status=str(credentials.status),
                updated_at=credentials.updated_at,
                connected_by=credentials.connected_by,
                last_refreshed_at=credentials.last_refreshed_at,
                revoked_at=credentials.revoked_at,
            )
        )

    async def delete(self, source_id: UUID) -> None:
        await self._db.execute(
            delete(DataSourceCredentialModel).where(DataSourceCredentialModel.data_source_id == source_id)
        )


def _sync_job(row: SyncJobModel) -> SyncJob:
    return SyncJob(
        id=row.id,
        data_source_id=row.data_source_id,
        trigger=SyncTrigger(row.trigger),
        mode=SyncMode(row.mode),
        status=JobStatus(row.status),
        queued_at=row.queued_at,
        updated_at=row.updated_at,
        requested_by=row.requested_by,
        cursor_before=row.cursor_before,
        cursor_after=row.cursor_after,
        stats=SyncStats.from_dict(row.stats),
        attempt=row.attempt,
        max_attempts=row.max_attempts,
        error_code=row.error_code,
        error_message=row.error_message,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


class SqlSyncJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session

    async def add(self, job: SyncJob) -> None:
        self._db.add(
            SyncJobModel(
                id=job.id,
                data_source_id=job.data_source_id,
                trigger=str(job.trigger),
                mode=str(job.mode),
                status=str(job.status),
                cursor_before=job.cursor_before,
                cursor_after=job.cursor_after,
                stats=job.stats.as_dict(),
                attempt=job.attempt,
                max_attempts=job.max_attempts,
                requested_by=job.requested_by,
                queued_at=job.queued_at,
                updated_at=job.updated_at,
            )
        )
        try:
            await self._db.flush()
        except IntegrityError as exc:
            if ACTIVE_JOB_INDEX in str(exc.orig):
                raise UniqueViolationError(ACTIVE_JOB_INDEX) from None
            raise

    async def get(self, job_id: UUID) -> SyncJob | None:
        row = await self._db.get(SyncJobModel, job_id, populate_existing=True)
        return _sync_job(row) if row is not None else None

    async def get_for_update(self, job_id: UUID) -> SyncJob | None:
        row = await self._db.get(SyncJobModel, job_id, with_for_update=True, populate_existing=True)
        return _sync_job(row) if row is not None else None

    async def save(self, job: SyncJob) -> None:
        await self._db.execute(
            update(SyncJobModel)
            .where(SyncJobModel.id == job.id)
            .values(
                mode=str(job.mode),
                status=str(job.status),
                cursor_before=job.cursor_before,
                cursor_after=job.cursor_after,
                stats=job.stats.as_dict(),
                attempt=job.attempt,
                error_code=job.error_code,
                error_message=job.error_message,
                started_at=job.started_at,
                finished_at=job.finished_at,
                updated_at=job.updated_at,
            )
        )

    async def set_task_id(self, job_id: UUID, task_id: str | None) -> None:
        await self._db.execute(
            update(SyncJobModel).where(SyncJobModel.id == job_id).values(celery_task_id=task_id)
        )

    async def active_for_source(self, source_id: UUID) -> SyncJob | None:
        row = await self._db.scalar(
            select(SyncJobModel)
            .where(SyncJobModel.data_source_id == source_id, SyncJobModel.status.in_(_ACTIVE))
            .execution_options(populate_existing=True)
        )
        return _sync_job(row) if row is not None else None

    async def unpublished(self, *, idle_since: datetime, limit: int) -> list[UUID]:
        rows = await self._db.scalars(
            select(SyncJobModel.id)
            .where(
                SyncJobModel.status == str(JobStatus.QUEUED),
                SyncJobModel.celery_task_id.is_(None),
                SyncJobModel.updated_at < idle_since,
            )
            .order_by(SyncJobModel.updated_at)
            .limit(limit)
        )
        return list(rows)

    async def stalled(self, *, idle_since: datetime, limit: int) -> list[UUID]:
        rows = await self._db.scalars(
            select(SyncJobModel.id)
            .where(SyncJobModel.status == str(JobStatus.RUNNING), SyncJobModel.updated_at < idle_since)
            .order_by(SyncJobModel.updated_at)
            .limit(limit)
        )
        return list(rows)

    async def page(self, query: JobQuery, page: PageRequest) -> Page[SyncJob]:
        statement = select(SyncJobModel)
        if query.status is not None:
            statement = statement.where(SyncJobModel.status == str(query.status))
        if query.source_ids is not None:
            statement = statement.where(SyncJobModel.data_source_id.in_(query.source_ids))
        return await paginate(self._db, statement, SyncJobModel, page, _sync_job)

    async def cancel_active(self, source_id: UUID, *, error_code: str, at: datetime) -> int:
        result = await self._db.execute(
            update(SyncJobModel)
            .where(SyncJobModel.data_source_id == source_id, SyncJobModel.status.in_(_ACTIVE))
            .values(status=str(JobStatus.CANCELLED), error_code=error_code, finished_at=at, updated_at=at)
        )
        return int(getattr(result, "rowcount", 0) or 0)
