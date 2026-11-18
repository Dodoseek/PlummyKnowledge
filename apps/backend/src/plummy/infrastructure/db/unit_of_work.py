"""SQLAlchemy implementation of the repositories and the unit of work (application/ports.py)."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from types import TracebackType
from typing import Any, Self
from uuid import UUID

from sqlalchemy import Select, delete, func, literal, or_, select, tuple_, union, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from plummy.application.pagination import Page, PageRequest, encode_cursor, escape_like
from plummy.application.ports import (
    ActivationRecord,
    ActivationTokenRepository,
    AuditLog,
    EncryptedSecret,
    GroupRepository,
    InstanceStateRepository,
    PermissionInfo,
    RoleInfo,
    RoleRepository,
    SecretRecord,
    SecretRepository,
    SessionRepository,
    SessionWithUser,
    SetChange,
    UniqueViolationError,
    UserQuery,
    UserRepository,
)
from plummy.core.ids import new_id
from plummy.core.logging import request_id_var
from plummy.domain.audit import AuditEntry
from plummy.domain.identity import (
    Group,
    InstanceState,
    SessionRevokeReason,
    SystemRole,
    User,
    UserSession,
    UserStatus,
)
from plummy.domain.secrets import SecretKind
from plummy.infrastructure.db.base import Base
from plummy.infrastructure.db.models import (
    ActivationTokenModel,
    AuditLogModel,
    GroupModel,
    GroupRoleModel,
    InstanceStateModel,
    PermissionModel,
    RoleModel,
    RolePermissionModel,
    SecretReferenceModel,
    SessionModel,
    UserGroupModel,
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


def _group(row: GroupModel) -> Group:
    return Group(
        id=row.id,
        name=row.name,
        description=row.description,
        created_at=row.created_at,
        deleted_at=row.deleted_at,
    )


async def _paginate[M: Base, D](
    db: AsyncSession,
    statement: Select[M],
    model: type[M],
    page: PageRequest,
    mapper: Callable[[M], D],
) -> Page[D]:
    """Keyset pagination over ``(sort column, id)`` (ADR-014)."""
    column: Any = getattr(model, page.sort.field)
    row_id: Any = getattr(model, "id")  # noqa: B009 - every paginated model has ``id``
    if page.after is not None:
        # Bind the cursor value with the column's type (e.g. citext) so it compares like the ORDER BY.
        key = tuple_(literal(page.after.value, type_=column.type), literal(page.after.id, type_=row_id.type))
        position = tuple_(column, row_id)
        statement = statement.where(position < key if page.sort.descending else position > key)
    order = (column.desc(), row_id.desc()) if page.sort.descending else (column.asc(), row_id.asc())
    rows = list((await db.scalars(statement.order_by(*order).limit(page.limit + 1))).all())
    next_cursor = None
    if len(rows) > page.limit:
        rows = rows[: page.limit]
        last = rows[-1]
        next_cursor = encode_cursor(page.sort, getattr(last, page.sort.field), getattr(last, "id"))  # noqa: B009
    return Page(items=[mapper(row) for row in rows], next_cursor=next_cursor)


def _search(pattern_source: str, *columns: Any) -> Any:
    pattern = f"%{escape_like(pattern_source)}%"
    return or_(*(column.ilike(pattern, escape="\\") for column in columns))


async def _flush_unique(db: AsyncSession, constraint: str) -> None:
    """Flush; a violation of ``constraint`` becomes :class:`UniqueViolationError` (other errors propagate)."""
    try:
        await db.flush()
    except IntegrityError as exc:
        if constraint in str(exc.orig):
            raise UniqueViolationError(constraint) from None
        raise


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

    async def get_for_update(self, user_id: UUID) -> User | None:
        row = await self._db.scalar(
            select(UserModel)
            .where(UserModel.id == user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return _user(row) if row is not None else None

    async def get_by_email(self, email: str) -> User | None:
        row = await self._db.scalar(
            select(UserModel).where(UserModel.email == email, UserModel.deleted_at.is_(None))
        )
        return _user(row) if row is not None else None

    async def page(self, query: UserQuery, page: PageRequest) -> Page[User]:
        statement = select(UserModel).where(UserModel.deleted_at.is_(None))
        if query.status is not None:
            statement = statement.where(UserModel.status == query.status.value)
        if query.group_id is not None:
            members = select(UserGroupModel.user_id).where(UserGroupModel.group_id == query.group_id)
            statement = statement.where(UserModel.id.in_(members))
        if query.search:
            statement = statement.where(_search(query.search, UserModel.email, UserModel.display_name))
        return await _paginate(self._db, statement, UserModel, page, _user)

    async def existing_ids(self, user_ids: frozenset[UUID]) -> frozenset[UUID]:
        if not user_ids:
            return frozenset()
        rows = await self._db.scalars(
            select(UserModel.id).where(UserModel.id.in_(user_ids), UserModel.deleted_at.is_(None))
        )
        return frozenset(rows.all())

    async def update(self, user: User, *, at: datetime) -> None:
        await self._db.execute(
            update(UserModel)
            .where(UserModel.id == user.id)
            .values(
                display_name=user.display_name,
                status=user.status.value,
                deleted_at=user.deleted_at,
                updated_at=at,
            )
        )

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
        await _flush_unique(self._db, "uq_users_email_active")

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
        direct = select(UserRoleModel.role_id.label("role_id")).where(UserRoleModel.user_id == user_id)
        via_groups = (
            select(GroupRoleModel.role_id.label("role_id"))
            .join(UserGroupModel, UserGroupModel.group_id == GroupRoleModel.group_id)
            .join(GroupModel, GroupModel.id == GroupRoleModel.group_id)
            .where(UserGroupModel.user_id == user_id, GroupModel.deleted_at.is_(None))
        )
        role_ids = union(direct, via_groups).subquery()
        rows = await self._db.execute(
            select(RoleModel.key, PermissionModel.key)
            .select_from(RoleModel)
            .join(role_ids, role_ids.c.role_id == RoleModel.id)
            .outerjoin(RolePermissionModel, RolePermissionModel.role_id == RoleModel.id)
            .outerjoin(PermissionModel, PermissionModel.id == RolePermissionModel.permission_id)
        )
        roles: set[str] = set()
        permissions: set[str] = set()
        for role_key, permission_key in rows:
            roles.add(role_key)
            if permission_key is not None:
                permissions.add(permission_key)
        return frozenset(roles), frozenset(permissions)

    async def catalog(self) -> list[RoleInfo]:
        roles = (await self._db.scalars(select(RoleModel).order_by(RoleModel.key))).all()
        links = await self._db.execute(
            select(RolePermissionModel.role_id, PermissionModel.key)
            .join(PermissionModel, PermissionModel.id == RolePermissionModel.permission_id)
            .order_by(PermissionModel.key)
        )
        by_role: dict[UUID, list[str]] = {}
        for role_id, key in links:
            by_role.setdefault(role_id, []).append(key)
        return [
            RoleInfo(
                key=role.key,
                name=role.name,
                description=role.description,
                is_system=role.is_system,
                permissions=tuple(by_role.get(role.id, [])),
            )
            for role in roles
        ]

    async def permission_catalog(self) -> list[PermissionInfo]:
        rows = await self._db.scalars(select(PermissionModel).order_by(PermissionModel.key))
        return [PermissionInfo(key=row.key, description=row.description) for row in rows]

    async def existing_keys(self) -> frozenset[str]:
        return frozenset((await self._db.scalars(select(RoleModel.key))).all())

    async def user_role_keys(self, user_id: UUID) -> frozenset[str]:
        return (await self.role_keys_for_users([user_id])).get(user_id, frozenset())

    async def role_keys_for_users(self, user_ids: list[UUID]) -> dict[UUID, frozenset[str]]:
        if not user_ids:
            return {}
        rows = await self._db.execute(
            select(UserRoleModel.user_id, RoleModel.key)
            .join(RoleModel, RoleModel.id == UserRoleModel.role_id)
            .where(UserRoleModel.user_id.in_(user_ids))
        )
        result: dict[UUID, set[str]] = {}
        for user_id, key in rows:
            result.setdefault(user_id, set()).add(key)
        return {user_id: frozenset(keys) for user_id, keys in result.items()}

    async def _role_ids(self, keys: frozenset[str]) -> dict[str, UUID]:
        if not keys:
            return {}
        rows = await self._db.execute(select(RoleModel.key, RoleModel.id).where(RoleModel.key.in_(keys)))
        found: dict[str, UUID] = {row.key: row.id for row in rows}
        missing = keys - found.keys()
        if missing:
            raise LookupError(f"roles do not exist: {sorted(missing)}")
        return found

    async def set_user_roles(
        self, user_id: UUID, role_keys: frozenset[str], *, granted_by: UUID | None
    ) -> SetChange[str]:
        current = await self.user_role_keys(user_id)
        added, removed = role_keys - current, current - role_keys
        if removed:
            ids = (await self._role_ids(removed)).values()
            await self._db.execute(
                delete(UserRoleModel).where(UserRoleModel.user_id == user_id, UserRoleModel.role_id.in_(ids))
            )
        for role_id in (await self._role_ids(added)).values():
            self._db.add(UserRoleModel(user_id=user_id, role_id=role_id, granted_by=granted_by))
        await self._db.flush()
        return SetChange(added=added, removed=removed)

    async def group_role_keys(self, group_id: UUID) -> frozenset[str]:
        rows = await self._db.scalars(
            select(RoleModel.key)
            .join(GroupRoleModel, GroupRoleModel.role_id == RoleModel.id)
            .where(GroupRoleModel.group_id == group_id)
        )
        return frozenset(rows.all())

    async def set_group_roles(
        self, group_id: UUID, role_keys: frozenset[str], *, granted_by: UUID | None
    ) -> SetChange[str]:
        current = await self.group_role_keys(group_id)
        added, removed = role_keys - current, current - role_keys
        if removed:
            ids = (await self._role_ids(removed)).values()
            await self._db.execute(
                delete(GroupRoleModel).where(
                    GroupRoleModel.group_id == group_id, GroupRoleModel.role_id.in_(ids)
                )
            )
        for role_id in (await self._role_ids(added)).values():
            self._db.add(GroupRoleModel(group_id=group_id, role_id=role_id, granted_by=granted_by))
        await self._db.flush()
        return SetChange(added=added, removed=removed)

    async def lock_administrators(self) -> None:
        await self._db.execute(
            select(RoleModel.id).where(RoleModel.key == SystemRole.ADMIN.value).with_for_update()
        )

    async def count_active_administrators(self, *, excluding: UUID | None = None) -> int:
        statement = (
            select(func.count(func.distinct(UserModel.id)))
            .select_from(UserRoleModel)
            .join(RoleModel, RoleModel.id == UserRoleModel.role_id)
            .join(UserModel, UserModel.id == UserRoleModel.user_id)
            .where(
                RoleModel.key == SystemRole.ADMIN.value,
                UserModel.status == UserStatus.ACTIVE.value,
                UserModel.deleted_at.is_(None),
            )
        )
        if excluding is not None:
            statement = statement.where(UserModel.id != excluding)
        return int(await self._db.scalar(statement) or 0)


class SqlGroupRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session

    async def add(self, group: Group) -> None:
        self._db.add(
            GroupModel(
                id=group.id,
                name=group.name,
                description=group.description,
                created_at=group.created_at,
                updated_at=group.created_at,
            )
        )
        await _flush_unique(self._db, "uq_groups_name_active")

    async def get(self, group_id: UUID) -> Group | None:
        row = await self._db.scalar(
            select(GroupModel).where(GroupModel.id == group_id, GroupModel.deleted_at.is_(None))
        )
        return _group(row) if row is not None else None

    async def get_by_name(self, name: str) -> Group | None:
        row = await self._db.scalar(
            select(GroupModel).where(GroupModel.name == name, GroupModel.deleted_at.is_(None))
        )
        return _group(row) if row is not None else None

    async def page(self, search: str | None, page: PageRequest) -> Page[Group]:
        statement = select(GroupModel).where(GroupModel.deleted_at.is_(None))
        if search:
            statement = statement.where(_search(search, GroupModel.name))
        return await _paginate(self._db, statement, GroupModel, page, _group)

    async def update(self, group: Group, *, at: datetime) -> None:
        try:
            await self._db.execute(
                update(GroupModel)
                .where(GroupModel.id == group.id)
                .values(
                    name=group.name, description=group.description, deleted_at=group.deleted_at, updated_at=at
                )
            )
        except IntegrityError as exc:
            if "uq_groups_name_active" in str(exc.orig):
                raise UniqueViolationError("uq_groups_name_active") from None
            raise

    async def members(self, group_id: UUID) -> list[User]:
        rows = await self._db.scalars(
            select(UserModel)
            .join(UserGroupModel, UserGroupModel.user_id == UserModel.id)
            .where(UserGroupModel.group_id == group_id, UserModel.deleted_at.is_(None))
            .order_by(UserModel.email)
        )
        return [_user(row) for row in rows]

    async def member_counts(self, group_ids: list[UUID]) -> dict[UUID, int]:
        if not group_ids:
            return {}
        rows = await self._db.execute(
            select(UserGroupModel.group_id, func.count())
            .join(UserModel, UserModel.id == UserGroupModel.user_id)
            .where(UserGroupModel.group_id.in_(group_ids), UserModel.deleted_at.is_(None))
            .group_by(UserGroupModel.group_id)
        )
        return {group_id: int(count) for group_id, count in rows}

    async def set_members(self, group_id: UUID, user_ids: frozenset[UUID]) -> SetChange[UUID]:
        current = frozenset(
            (
                await self._db.scalars(
                    select(UserGroupModel.user_id).where(UserGroupModel.group_id == group_id)
                )
            ).all()
        )
        added, removed = user_ids - current, current - user_ids
        if removed:
            await self._db.execute(
                delete(UserGroupModel).where(
                    UserGroupModel.group_id == group_id, UserGroupModel.user_id.in_(removed)
                )
            )
        for user_id in added:
            self._db.add(UserGroupModel(user_id=user_id, group_id=group_id))
        await self._db.flush()
        return SetChange(added=added, removed=removed)

    async def groups_of_user(self, user_id: UUID) -> list[Group]:
        rows = await self._db.scalars(
            select(GroupModel)
            .join(UserGroupModel, UserGroupModel.group_id == GroupModel.id)
            .where(UserGroupModel.user_id == user_id, GroupModel.deleted_at.is_(None))
            .order_by(GroupModel.name)
        )
        return [_group(row) for row in rows]

    async def remove_user_from_all(self, user_id: UUID) -> None:
        await self._db.execute(delete(UserGroupModel).where(UserGroupModel.user_id == user_id))

    async def clear(self, group_id: UUID) -> None:
        await self._db.execute(delete(UserGroupModel).where(UserGroupModel.group_id == group_id))
        await self._db.execute(delete(GroupRoleModel).where(GroupRoleModel.group_id == group_id))


class SqlActivationTokenRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session

    async def put(
        self, user_id: UUID, token_hash: bytes, *, expires_at: datetime, created_by: UUID | None
    ) -> None:
        statement = pg_insert(ActivationTokenModel).values(
            user_id=user_id, token_hash=token_hash, expires_at=expires_at, created_by=created_by
        )
        await self._db.execute(
            statement.on_conflict_do_update(
                index_elements=[ActivationTokenModel.user_id],
                set_={
                    "token_hash": statement.excluded.token_hash,
                    "expires_at": statement.excluded.expires_at,
                    "created_by": statement.excluded.created_by,
                    "created_at": func.now(),
                },
            )
        )

    async def find(self, token_hash: bytes) -> ActivationRecord | None:
        row = (
            await self._db.execute(
                select(ActivationTokenModel.user_id, ActivationTokenModel.expires_at).where(
                    ActivationTokenModel.token_hash == token_hash
                )
            )
        ).one_or_none()
        return ActivationRecord(user_id=row[0], expires_at=row[1]) if row is not None else None

    async def delete(self, user_id: UUID) -> None:
        await self._db.execute(delete(ActivationTokenModel).where(ActivationTokenModel.user_id == user_id))


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

    async def get(self, session_id: UUID) -> UserSession | None:
        row = await self._db.get(SessionModel, session_id)
        return _session(row) if row is not None else None

    async def list_for_user(self, user_id: UUID) -> list[UserSession]:
        rows = await self._db.scalars(
            select(SessionModel)
            .where(SessionModel.user_id == user_id, SessionModel.revoked_at.is_(None))
            .order_by(SessionModel.last_seen_at.desc(), SessionModel.id)
        )
        return [_session(row) for row in rows]

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


def _secret(row: SecretReferenceModel) -> SecretRecord:
    return SecretRecord(
        id=row.id,
        kind=SecretKind(row.kind),
        encrypted=EncryptedSecret(
            key_id=row.key_id, algorithm=row.algorithm, nonce=row.nonce, ciphertext=row.ciphertext
        ),
        hint=row.hint,
    )


class SqlSecretRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, record: SecretRecord, *, at: datetime) -> None:
        self._session.add(
            SecretReferenceModel(
                id=record.id,
                kind=record.kind.value,
                ciphertext=record.encrypted.ciphertext,
                nonce=record.encrypted.nonce,
                key_id=record.encrypted.key_id,
                algorithm=record.encrypted.algorithm,
                hint=record.hint,
                created_at=at,
                updated_at=at,
            )
        )
        await self._session.flush()

    async def get(self, secret_id: UUID) -> SecretRecord | None:
        row = await self._session.get(SecretReferenceModel, secret_id)
        return _secret(row) if row is not None else None

    async def get_for_update(self, secret_id: UUID) -> SecretRecord | None:
        row = await self._session.scalar(
            select(SecretReferenceModel)
            .where(SecretReferenceModel.id == secret_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return _secret(row) if row is not None else None

    async def _write(self, secret_id: UUID, values: dict[str, Any]) -> None:
        await self._session.execute(
            update(SecretReferenceModel).where(SecretReferenceModel.id == secret_id).values(**values)
        )

    async def replace(
        self, secret_id: UUID, encrypted: EncryptedSecret, *, hint: str | None, at: datetime
    ) -> None:
        await self._write(
            secret_id,
            {
                "ciphertext": encrypted.ciphertext,
                "nonce": encrypted.nonce,
                "key_id": encrypted.key_id,
                "algorithm": encrypted.algorithm,
                "hint": hint,
                "updated_at": at,
            },
        )

    async def reencrypt(self, secret_id: UUID, encrypted: EncryptedSecret, *, at: datetime) -> None:
        await self._write(
            secret_id,
            {
                "ciphertext": encrypted.ciphertext,
                "nonce": encrypted.nonce,
                "key_id": encrypted.key_id,
                "algorithm": encrypted.algorithm,
                "rotated_at": at,
            },
        )

    async def delete(self, secret_id: UUID) -> None:
        await self._session.execute(delete(SecretReferenceModel).where(SecretReferenceModel.id == secret_id))

    async def count_by_key_id(self) -> dict[str, int]:
        rows = await self._session.execute(
            select(SecretReferenceModel.key_id, func.count().label("total")).group_by(
                SecretReferenceModel.key_id
            )
        )
        return {row.key_id: row.total for row in rows}

    async def ids_not_on_key(self, key_id: str, *, after: UUID | None, limit: int) -> list[UUID]:
        statement = select(SecretReferenceModel.id).where(SecretReferenceModel.key_id != key_id)
        if after is not None:
            statement = statement.where(SecretReferenceModel.id > after)
        result = await self._session.scalars(statement.order_by(SecretReferenceModel.id).limit(limit))
        return list(result.all())


class SqlAlchemyUnitOfWork:
    users: UserRepository
    roles: RoleRepository
    groups: GroupRepository
    activations: ActivationTokenRepository
    sessions: SessionRepository
    instance: InstanceStateRepository
    secrets: SecretRepository
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
        self.groups = SqlGroupRepository(session)
        self.activations = SqlActivationTokenRepository(session)
        self.sessions = SqlSessionRepository(session)
        self.instance = SqlInstanceStateRepository(session)
        self.secrets = SqlSecretRepository(session)
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
