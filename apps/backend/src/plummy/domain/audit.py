"""Audit log entries (DATABASE_SCHEMA.md §7.3, SECURITY_MODEL.md §14)."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from uuid import UUID


class ActorType(StrEnum):
    USER = "user"
    ANONYMOUS = "anonymous"
    SYSTEM = "system"  # server-side CLI
    WORKER = "worker"


class AuditOutcome(StrEnum):
    SUCCESS = "success"
    DENIED = "denied"
    FAILURE = "failure"


class AuditAction(StrEnum):
    SETUP_TOKEN_ISSUED = "setup.token_issued"  # noqa: S105 - an event name, not a secret
    SETUP_COMPLETED = "setup.completed"
    SETUP_INITIALIZE_REJECTED = "setup.initialize.rejected"
    AUTH_LOGIN_SUCCEEDED = "auth.login.succeeded"
    AUTH_LOGIN_FAILED = "auth.login.failed"
    AUTH_LOGOUT = "auth.logout"
    ADMIN_CREATED_VIA_CLI = "admin.created_via_cli"
    ADMIN_PASSWORD_RESET_VIA_CLI = "admin.password_reset_via_cli"  # noqa: S105 - an event name
    AUTH_SESSION_REVOKED = "auth.session.revoked"
    AUTH_SESSION_REVOKED_BY_ADMIN = "auth.session.revoked_by_admin"
    AUTH_PASSWORD_CHANGED = "auth.password.changed"  # noqa: S105 - an event name
    USER_CREATED = "user.created"
    USER_UPDATED = "user.updated"
    USER_DISABLED = "user.disabled"
    USER_DELETED = "user.deleted"
    USER_ACTIVATED = "user.activated"
    USER_ACTIVATION_REISSUED = "user.activation.reissued"
    USER_ACTIVATION_REJECTED = "user.activation.rejected"
    ROLE_ASSIGNED = "role.assigned"
    ROLE_REVOKED = "role.revoked"
    GROUP_CREATED = "group.created"
    GROUP_UPDATED = "group.updated"
    GROUP_DELETED = "group.deleted"
    GROUP_MEMBERS_CHANGED = "group.members.changed"
    SECRETS_ROTATED = "secrets.rotated"


@dataclass(frozen=True, slots=True)
class AuditEntry:
    """Never put secrets, tokens, passwords, plain-text emails or document content into ``details``."""

    action: AuditAction
    outcome: AuditOutcome
    actor_type: ActorType
    actor_user_id: UUID | None = None
    target_type: str | None = None
    target_id: UUID | None = None
    ip_address: str | None = None
    user_agent: str | None = None
    details: dict[str, str | int | bool] = field(default_factory=dict)
