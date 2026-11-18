"""Ports (interfaces) implemented by infrastructure adapters (ARCHITECTURE.md §5)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Protocol, Self
from uuid import UUID

from plummy.domain.audit import AuditEntry
from plummy.domain.identity import InstanceState, SessionRevokeReason, User, UserSession


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


class UserRepository(Protocol):
    async def get(self, user_id: UUID) -> User | None: ...

    async def get_by_email(self, email: str) -> User | None:
        """Lookup by canonical email, including disabled and soft-deleted users."""
        ...

    async def add(self, user: User) -> None: ...

    async def record_login(self, user_id: UUID, *, at: datetime, new_password_hash: str | None) -> None: ...

    async def set_password(self, user_id: UUID, *, password_hash: str, changed_at: datetime) -> None: ...


class RoleRepository(Protocol):
    async def assign(self, user_id: UUID, role_key: str, *, granted_by: UUID | None) -> None: ...

    async def roles_and_permissions(self, user_id: UUID) -> tuple[frozenset[str], frozenset[str]]:
        """Role keys and the union of their permission keys, in one query."""
        ...


class SessionRepository(Protocol):
    async def add(self, session: UserSession, token_hash: bytes) -> None: ...

    async def get_by_token_hash(self, token_hash: bytes) -> SessionWithUser | None: ...

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
