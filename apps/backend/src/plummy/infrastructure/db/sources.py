"""Credentials and sync jobs of external sources on PostgreSQL (DATABASE_SCHEMA.md §5.5, §7.2, ADR-020)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from plummy.domain.documents import JobStatus
from plummy.domain.sources import CredentialStatus, SourceCredentials
from plummy.infrastructure.db.models import DataSourceCredentialModel, SyncJobModel

_ACTIVE = (str(JobStatus.QUEUED), str(JobStatus.RUNNING))


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


class SqlSyncJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session

    async def cancel_active(self, source_id: UUID, *, error_code: str, at: datetime) -> int:
        result = await self._db.execute(
            update(SyncJobModel)
            .where(SyncJobModel.data_source_id == source_id, SyncJobModel.status.in_(_ACTIVE))
            .values(status=str(JobStatus.CANCELLED), error_code=error_code, finished_at=at, updated_at=at)
        )
        return int(getattr(result, "rowcount", 0) or 0)
