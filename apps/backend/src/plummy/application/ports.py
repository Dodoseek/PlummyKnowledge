"""Ports (interfaces) implemented by infrastructure adapters (ARCHITECTURE.md §5)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from plummy.application.pagination import Page, PageRequest
from plummy.domain.audit import AuditEntry
from plummy.domain.identity import Group, InstanceState, SessionRevokeReason, User, UserSession, UserStatus


class Clock(Protocol):
    def now(self) -> datetime:
        """Current time, timezone-aware UTC."""
        ...


class PasswordHasher(Protocol):
    async def hash(self, password: str) -> str: ...

    async def verify(self, password_hash: str | None, password: str) -> bool:
        """``False`` for a mismatch. With ``None`` it still does the same amount of work (timing safety)."""
        ...

    def needs_rehash(self, password_hash: str) -> bool: ...


@dataclass(frozen=True, slots=True)
class RateLimitStatus:
    allowed: bool
    retry_after_seconds: int


class UniqueViolationError(Exception):
    """A concurrent transaction inserted the same unique value first (email, group name)."""


class RateLimiterUnavailableError(Exception):
    """The counter store is unreachable; callers fail closed."""


class RateLimiter(Protocol):
    """Fixed-window counters."""

    async def hit(self, key: str, *, limit: int, window_seconds: int) -> RateLimitStatus:
        """Count one event and report whether it is within the limit."""
        ...

    async def peek(self, key: str, *, limit: int, window_seconds: int) -> RateLimitStatus:
        """Report whether the limit is already exhausted, without counting."""
        ...

    async def reset(self, key: str) -> None: ...


class BootstrapTokenStore(Protocol):
    """Hands the plain bootstrap token to the server operator (a file readable only on the server)."""

    @property
    def location(self) -> str: ...

    def read(self) -> str | None:
        """The token currently in the store, or ``None``."""
        ...

    def write(self, token: str) -> None: ...

    def remove(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class SessionWithUser:
    session: UserSession
    user: User


@dataclass(frozen=True, slots=True)
class UserQuery:
    """Allowlisted filters of ``GET /users`` (ADR-014). Soft-deleted users are never listed."""

    status: UserStatus | None = None
    group_id: UUID | None = None
    search: str | None = None


@dataclass(frozen=True, slots=True)
class RoleInfo:
    key: str
    name: str
    description: str
    is_system: bool
    permissions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class PermissionInfo:
    key: str
    description: str


@dataclass(frozen=True, slots=True)
class SetChange[K]:
    """Result of replacing a set (roles, members): what was added and what was removed."""

    added: frozenset[K]
    removed: frozenset[K]


@dataclass(frozen=True, slots=True)
class ActivationRecord:
    user_id: UUID
    expires_at: datetime


class UserRepository(Protocol):
    async def get(self, user_id: UUID) -> User | None:
        """By id, including soft-deleted users (callers decide what a deleted user means)."""
        ...

    async def get_for_update(self, user_id: UUID) -> User | None:
        """Lock the user row until the end of the transaction."""
        ...

    async def get_by_email(self, email: str) -> User | None:
        """Lookup by canonical email among users that are not soft-deleted (disabled ones included)."""
        ...

    async def page(self, query: UserQuery, page: PageRequest) -> Page[User]: ...

    async def existing_ids(self, user_ids: frozenset[UUID]) -> frozenset[UUID]:
        """The subset of ids that belong to users that are not soft-deleted."""
        ...

    async def update(self, user: User, *, at: datetime) -> None:
        """Persist ``display_name``, ``status`` and ``deleted_at``."""
        ...

    async def add(self, user: User) -> None: ...

    async def record_login(self, user_id: UUID, *, at: datetime, new_password_hash: str | None) -> None: ...

    async def set_password(self, user_id: UUID, *, password_hash: str, changed_at: datetime) -> None: ...


class RoleRepository(Protocol):
    async def assign(self, user_id: UUID, role_key: str, *, granted_by: UUID | None) -> None: ...

    async def roles_and_permissions(self, user_id: UUID) -> tuple[frozenset[str], frozenset[str]]:
        """Effective role keys (direct and through groups that are not deleted) and their permissions."""
        ...

    async def catalog(self) -> list[RoleInfo]: ...

    async def permission_catalog(self) -> list[PermissionInfo]: ...

    async def existing_keys(self) -> frozenset[str]: ...

    async def user_role_keys(self, user_id: UUID) -> frozenset[str]:
        """Roles assigned to the user directly."""
        ...

    async def role_keys_for_users(self, user_ids: list[UUID]) -> dict[UUID, frozenset[str]]: ...

    async def set_user_roles(
        self, user_id: UUID, role_keys: frozenset[str], *, granted_by: UUID | None
    ) -> SetChange[str]: ...

    async def group_role_keys(self, group_id: UUID) -> frozenset[str]: ...

    async def set_group_roles(
        self, group_id: UUID, role_keys: frozenset[str], *, granted_by: UUID | None
    ) -> SetChange[str]: ...

    async def lock_administrators(self) -> None:
        """Serialize every change that can remove an administrator (row lock on the ``admin`` role)."""
        ...

    async def count_active_administrators(self, *, excluding: UUID | None = None) -> int:
        """Active, not deleted users with the ``admin`` role assigned directly."""
        ...


class GroupRepository(Protocol):
    async def add(self, group: Group) -> None: ...

    async def get(self, group_id: UUID) -> Group | None:
        """A group that is not soft-deleted."""
        ...

    async def get_by_name(self, name: str) -> Group | None: ...

    async def page(self, search: str | None, page: PageRequest) -> Page[Group]: ...

    async def update(self, group: Group, *, at: datetime) -> None:
        """Persist ``name``, ``description`` and ``deleted_at``."""
        ...

    async def members(self, group_id: UUID) -> list[User]:
        """Members that are not soft-deleted, ordered by email."""
        ...

    async def member_counts(self, group_ids: list[UUID]) -> dict[UUID, int]: ...

    async def set_members(self, group_id: UUID, user_ids: frozenset[UUID]) -> SetChange[UUID]: ...

    async def groups_of_user(self, user_id: UUID) -> list[Group]: ...

    async def remove_user_from_all(self, user_id: UUID) -> None: ...

    async def clear(self, group_id: UUID) -> None:
        """Remove all members and roles of the group."""
        ...


class ActivationTokenRepository(Protocol):
    async def put(
        self, user_id: UUID, token_hash: bytes, *, expires_at: datetime, created_by: UUID | None
    ) -> None:
        """Store the user's only activation token, replacing an earlier one."""
        ...

    async def find(self, token_hash: bytes) -> ActivationRecord | None: ...

    async def delete(self, user_id: UUID) -> None: ...


class SessionRepository(Protocol):
    async def add(self, session: UserSession, token_hash: bytes) -> None: ...

    async def get_by_token_hash(self, token_hash: bytes) -> SessionWithUser | None: ...

    async def get(self, session_id: UUID) -> UserSession | None: ...

    async def list_for_user(self, user_id: UUID) -> list[UserSession]:
        """Sessions of the user that are not revoked, most recently used first."""
        ...

    async def touch(self, session_id: UUID, *, at: datetime) -> None: ...

    async def revoke(self, session_id: UUID, *, reason: SessionRevokeReason, at: datetime) -> bool:
        """Revoke if still active; ``True`` if this call revoked it."""
        ...

    async def revoke_by_token_hash(
        self, token_hash: bytes, *, reason: SessionRevokeReason, at: datetime
    ) -> UUID | None: ...

    async def revoke_all_for_user(
        self, user_id: UUID, *, reason: SessionRevokeReason, at: datetime
    ) -> int: ...


class InstanceStateRepository(Protocol):
    async def get(self) -> InstanceState: ...

    async def get_for_update(self) -> InstanceState:
        """Lock the singleton row until the end of the transaction (serializes setup)."""
        ...

    async def save(self, state: InstanceState) -> None: ...


class AuditLog(Protocol):
    async def record(self, entry: AuditEntry) -> None: ...


class UnitOfWork(Protocol):
    """One database transaction. Leaving the context without ``commit()`` rolls back."""

    users: UserRepository
    roles: RoleRepository
    groups: GroupRepository
    activations: ActivationTokenRepository
    sessions: SessionRepository
    instance: InstanceStateRepository
    audit: AuditLog

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...


UnitOfWorkFactory = Callable[[], UnitOfWork]
