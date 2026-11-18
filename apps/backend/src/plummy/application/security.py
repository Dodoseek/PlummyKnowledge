"""Security context of an authenticated request and permission checks (ARCHITECTURE.md §9.1).

The context is built only from a verified server-side session; nothing in it comes from the client.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from plummy.application.errors import PermissionDeniedError
from plummy.domain.identity import Permission


@dataclass(frozen=True, slots=True)
class SecurityContext:
    user_id: UUID
    session_id: UUID
    email: str
    display_name: str
    roles: frozenset[str]
    permissions: frozenset[str]
    session_expires_at: datetime
    session_idle_expires_at: datetime

    def has_permission(self, permission: Permission) -> bool:
        return permission.value in self.permissions

    def require(self, *permissions: Permission) -> None:
        """Raise :class:`PermissionDeniedError` unless the user has every listed permission."""
        if not all(self.has_permission(permission) for permission in permissions):
            raise PermissionDeniedError("You do not have permission to perform this action.")


@dataclass(frozen=True, slots=True)
class ClientInfo:
    """Request metadata for audit and rate limiting. ``ip_address`` is the address after trusted proxies."""

    ip_address: str | None = None
    user_agent: str | None = None
