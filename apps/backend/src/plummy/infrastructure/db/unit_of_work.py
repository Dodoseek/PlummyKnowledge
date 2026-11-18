"""SQLAlchemy implementation of the repositories and the unit of work (application/ports.py)."""

from __future__ import annotations

from datetime import datetime
from types import TracebackType
from typing import Self
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plummy.application.ports import (
    AuditLog,
    InstanceStateRepository,
    RoleRepository,
    SessionRepository,
    SessionWithUser,
    UserRepository,
)
from plummy.core.ids import new_id
from plummy.core.logging import request_id_var
from plummy.domain.audit import AuditEntry
from plummy.domain.identity import InstanceState, SessionRevokeReason, User, UserSession, UserStatus
from plummy.infrastructure.db.models import (
    AuditLogModel,
    InstanceStateModel,
    PermissionModel,
    RoleModel,
    RolePermissionModel,
    SessionModel,
    UserModel,
    UserRoleModel,
)

INSTANCE_STATE_ID = 1


def _user(row: UserModel) -> User:
    return User(
        id=row.id,
        email=row.email,
        display_name=row.display_name,
        password_hash=row.password_hash,
        status=UserStatus(row.status),
        created_at=row.created_at,
        password_changed_at=row.password_changed_at,
        last_login_at=row.last_login_at,
        deleted_at=row.deleted_at,
    )


def _session(row: SessionModel) -> UserSession:
    return UserSession(
        id=row.id,
        user_id=row.user_id,
        created_at=row.created_at,
        last_seen_at=row.last_seen_at,
        expires_at=row.expires_at,
        revoked_at=row.revoked_at,
        revoke_reason=SessionRevokeReason(row.revoke_reason) if row.revoke_reason else None,
        user_agent=row.user_agent,
        ip_address=str(row.ip_address) if row.ip_address is not None else None,
    )


class SqlUserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session

    async def get(self, user_id: UUID) -> User | None:
        row = await self._db.get(UserModel, user_id)
        return _user(row) if row is not None else None

    async def get_by_email(self, email: str) -> User | None:
        row = await self._db.scalar(select(UserModel).where(UserModel.email == email))
        return _user(row) if row is not None else None

    async def add(self, user: User) -> None:
        self._db.add(
            UserModel(
                id=user.id,
                email=user.email,
                display_name=user.display_name,
                password_hash=user.password_hash,
                status=user.status.value,
                created_at=user.created_at,
                updated_at=user.created_at,
                password_changed_at=user.password_changed_at,
                last_login_at=user.last_login_at,
                deleted_at=user.deleted_at,
            )
        )
        await self._db.flush()

    async def record_login(self, user_id: UUID, *, at: datetime, new_password_hash: str | None) -> None:
        values: dict[str, object] = {"last_login_at": at, "updated_at": at}
        if new_password_hash is not None:
            # Re-hash with current parameters; not a password change, so sessions stay valid.
            values["password_hash"] = new_password_hash
        await self._db.execute(update(UserModel).where(UserModel.id == user_id).values(**values))

    async def set_password(self, user_id: UUID, *, password_hash: str, changed_at: datetime) -> None:
        await self._db.execute(
            update(UserModel)
            .where(UserModel.id == user_id)
            .values(password_hash=password_hash, password_changed_at=changed_at, updated_at=changed_at)
        )


class SqlRoleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session

    async def assign(self, user_id: UUID, role_key: str, *, granted_by: UUID | None) -> None:
        role_id = await self._db.scalar(select(RoleModel.id).where(RoleModel.key == role_key))
        if role_id is None:
            raise LookupError(f"role {role_key!r} does not exist")
        self._db.add(UserRoleModel(user_id=user_id, role_id=role_id, granted_by=granted_by))
        await self._db.flush()

    async def roles_and_permissions(self, user_id: UUID) -> tuple[frozenset[str], frozenset[str]]:
        rows = await self._db.execute(
            select(RoleModel.key, PermissionModel.key)
            .select_from(UserRoleModel)
            .join(RoleModel, RoleModel.id == UserRoleModel.role_id)
            .outerjoin(RolePermissionModel, RolePermissionModel.role_id == RoleModel.id)
            .outerjoin(PermissionModel, PermissionModel.id == RolePermissionModel.permission_id)
            .where(UserRoleModel.user_id == user_id)
        )
        roles: set[str] = set()
        permissions: set[str] = set()
        for role_key, permission_key in rows:
            roles.add(role_key)
            if permission_key is not None:
                permissions.add(permission_key)
        return frozenset(roles), frozenset(permissions)


class SqlSessionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session

    async def add(self, session: UserSession, token_hash: bytes) -> None:
        self._db.add(
            SessionModel(
                id=session.id,
                user_id=session.user_id,
                token_hash=token_hash,
                created_at=session.created_at,
                last_seen_at=session.last_seen_at,
                expires_at=session.expires_at,
                user_agent=session.user_agent,
                ip_address=session.ip_address,
            )
        )
        await self._db.flush()

    async def get_by_token_hash(self, token_hash: bytes) -> SessionWithUser | None:
        row = (
            await self._db.execute(
                select(SessionModel, UserModel)
                .join(UserModel, UserModel.id == SessionModel.user_id)
                .where(SessionModel.token_hash == token_hash)
            )
        ).one_or_none()
        if row is None:
            return None
        return SessionWithUser(session=_session(row[0]), user=_user(row[1]))

    async def touch(self, session_id: UUID, *, at: datetime) -> None:
        await self._db.execute(
            update(SessionModel)
            .where(SessionModel.id == session_id, SessionModel.revoked_at.is_(None))
            .values(last_seen_at=at)
        )

    async def revoke(self, session_id: UUID, *, reason: SessionRevokeReason, at: datetime) -> bool:
        result = await self._db.execute(
            update(SessionModel)
            .where(SessionModel.id == session_id, SessionModel.revoked_at.is_(None))
            .values(revoked_at=at, revoke_reason=reason.value)
            .returning(SessionModel.id)
        )
        return result.scalar_one_or_none() is not None

    async def revoke_by_token_hash(
        self, token_hash: bytes, *, reason: SessionRevokeReason, at: datetime
    ) -> UUID | None:
        result = await self._db.execute(
            update(SessionModel)
            .where(SessionModel.token_hash == token_hash, SessionModel.revoked_at.is_(None))
            .values(revoked_at=at, revoke_reason=reason.value)
            .returning(SessionModel.id)
        )
        return result.scalar_one_or_none()

    async def revoke_all_for_user(self, user_id: UUID, *, reason: SessionRevokeReason, at: datetime) -> int:
        result = await self._db.execute(
            update(SessionModel)
            .where(SessionModel.user_id == user_id, SessionModel.revoked_at.is_(None))
            .values(revoked_at=at, revoke_reason=reason.value)
            .returning(SessionModel.id)
        )
        return len(result.scalars().all())


class SqlInstanceStateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session

    async def _load(self, *, for_update: bool) -> InstanceStateModel:
        statement = select(InstanceStateModel).where(InstanceStateModel.id == INSTANCE_STATE_ID)
        if for_update:
            statement = statement.with_for_update()
        row = await self._db.scalar(statement.execution_options(populate_existing=True))
        if row is None:
            # Created by migration 0002; its absence means the schema is broken, not "uninitialized".
            raise RuntimeError("instance_state row is missing")
        return row

    async def get(self) -> InstanceState:
        return self._domain(await self._load(for_update=False))

    async def get_for_update(self) -> InstanceState:
        return self._domain(await self._load(for_update=True))

    async def save(self, state: InstanceState) -> None:
        await self._db.execute(
            update(InstanceStateModel)
            .where(InstanceStateModel.id == INSTANCE_STATE_ID)
            .values(
                initialized_at=state.initialized_at,
                initialized_by_user_id=state.initialized_by_user_id,
                bootstrap_token_hash=state.bootstrap_token_hash,
                bootstrap_token_expires_at=state.bootstrap_token_expires_at,
                bootstrap_token_used_at=state.bootstrap_token_used_at,
                updated_at=func.now(),
            )
        )

    @staticmethod
    def _domain(row: InstanceStateModel) -> InstanceState:
        return InstanceState(
            initialized_at=row.initialized_at,
            initialized_by_user_id=row.initialized_by_user_id,
            bootstrap_token_hash=row.bootstrap_token_hash,
            bootstrap_token_expires_at=row.bootstrap_token_expires_at,
            bootstrap_token_used_at=row.bootstrap_token_used_at,
        )


class SqlAuditLog:
    """Append-only: only INSERT (DATABASE_SCHEMA.md §7.3)."""

    def __init__(self, session: AsyncSession) -> None:
        self._db = session

    async def record(self, entry: AuditEntry) -> None:
        self._db.add(
            AuditLogModel(
                id=new_id(),
                actor_type=entry.actor_type.value,
                actor_user_id=entry.actor_user_id,
                action=entry.action.value,
                target_type=entry.target_type,
                target_id=entry.target_id,
                outcome=entry.outcome.value,
                correlation_id=request_id_var.get(),
                ip_address=entry.ip_address,
                user_agent=entry.user_agent[:512] if entry.user_agent else None,
                details=dict(entry.details),
            )
        )
        await self._db.flush()


class SqlAlchemyUnitOfWork:
    users: UserRepository
    roles: RoleRepository
    sessions: SessionRepository
    instance: InstanceStateRepository
    audit: AuditLog

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory
        self._session: AsyncSession | None = None

    async def __aenter__(self) -> Self:
        session = self._session_factory()
        await session.begin()
        self._session = session
        self.users = SqlUserRepository(session)
        self.roles = SqlRoleRepository(session)
        self.sessions = SqlSessionRepository(session)
        self.instance = SqlInstanceStateRepository(session)
        self.audit = SqlAuditLog(session)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        session = self._session
        self._session = None
        if session is None:
            return
        try:
            if session.in_transaction():
                await session.rollback()
        finally:
            await session.close()

    async def commit(self) -> None:
        if self._session is None:
            raise RuntimeError("unit of work is not active")
        await self._session.commit()


class SqlAlchemyUnitOfWorkFactory:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    def __call__(self) -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(self._session_factory)
