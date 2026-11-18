"""Identity entities and their rules. Plain dataclasses: persistence lives in infrastructure."""

from __future__ import annotations

import hmac
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

MAX_DISPLAY_NAME_LENGTH = 200
MAX_USER_AGENT_LENGTH = 512


class UserStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"
    INVITED = "invited"


class SessionRevokeReason(StrEnum):
    LOGOUT = "logout"
    SUPERSEDED = "superseded"
    ADMIN = "admin"
    PASSWORD_CHANGE = "password_change"  # noqa: S105 - a revoke reason, not a secret
    USER_DISABLED = "user_disabled"


class SessionState(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"  # also: the user can no longer sign in, or the password changed after login


class InvalidDisplayNameError(ValueError):
    pass


def normalize_display_name(value: str) -> str:
    """Trimmed display name: 1-200 characters, no control characters."""
    name = value.strip()
    if not name or len(name) > MAX_DISPLAY_NAME_LENGTH:
        raise InvalidDisplayNameError(f"Display name must be 1-{MAX_DISPLAY_NAME_LENGTH} characters long.")
    if any(unicodedata.category(char) in {"Cc", "Cf", "Zl", "Zp"} for char in name):
        raise InvalidDisplayNameError("Display name must not contain control characters.")
    return name


@dataclass(slots=True)
class User:
    id: UUID
    email: str
    display_name: str
    password_hash: str | None
    status: UserStatus
    created_at: datetime
    password_changed_at: datetime | None = None
    last_login_at: datetime | None = None
    deleted_at: datetime | None = None

    @property
    def can_sign_in(self) -> bool:
        return self.status is UserStatus.ACTIVE and self.deleted_at is None and self.password_hash is not None


@dataclass(slots=True)
class UserSession:
    id: UUID
    user_id: UUID
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    revoked_at: datetime | None = None
    revoke_reason: SessionRevokeReason | None = None
    user_agent: str | None = None
    ip_address: str | None = None

    def idle_expires_at(self, idle_ttl: timedelta) -> datetime:
        return min(self.last_seen_at + idle_ttl, self.expires_at)

    def state(self, user: User, *, now: datetime, idle_ttl: timedelta) -> SessionState:
        """ADR-013 §1: revoked, absolutely or idly expired, inactive user, password changed after login."""
        if self.revoked_at is not None:
            return SessionState.REVOKED
        if now >= self.expires_at or now >= self.last_seen_at + idle_ttl:
            return SessionState.EXPIRED
        if user.status is not UserStatus.ACTIVE or user.deleted_at is not None:
            return SessionState.EXPIRED
        if user.password_changed_at is not None and self.created_at < user.password_changed_at:
            return SessionState.EXPIRED
        return SessionState.ACTIVE


@dataclass(slots=True)
class InstanceState:
    """The singleton row with the instance's setup state (ADR-006, DATABASE_SCHEMA.md §3.0)."""

    initialized_at: datetime | None
    initialized_by_user_id: UUID | None
    bootstrap_token_hash: bytes | None
    bootstrap_token_expires_at: datetime | None
    bootstrap_token_used_at: datetime | None

    @property
    def is_initialized(self) -> bool:
        return self.initialized_at is not None

    def has_usable_bootstrap_token(self, *, now: datetime) -> bool:
        return (
            not self.is_initialized
            and self.bootstrap_token_hash is not None
            and self.bootstrap_token_used_at is None
            and self.bootstrap_token_expires_at is not None
            and now < self.bootstrap_token_expires_at
        )

    def accepts_bootstrap_token(self, token_hash: bytes, *, now: datetime) -> bool:
        """Constant-time comparison; an expired, used or absent token never matches."""
        stored = self.bootstrap_token_hash or b"\x00" * 32
        matches = hmac.compare_digest(stored, token_hash)
        return matches and self.has_usable_bootstrap_token(now=now)

    def issue_bootstrap_token(self, token_hash: bytes, *, expires_at: datetime) -> None:
        if self.is_initialized:
            raise ValueError("instance is already initialized")
        self.bootstrap_token_hash = token_hash
        self.bootstrap_token_expires_at = expires_at
        self.bootstrap_token_used_at = None

    def mark_initialized(self, *, by_user_id: UUID, now: datetime) -> None:
        if self.is_initialized:
            raise ValueError("instance is already initialized")
        self.initialized_at = now
        self.initialized_by_user_id = by_user_id
        # The token is spent: its hash is erased so it can never be checked again.
        if self.bootstrap_token_hash is not None:
            self.bootstrap_token_used_at = now
        self.bootstrap_token_hash = None
        self.bootstrap_token_expires_at = None
