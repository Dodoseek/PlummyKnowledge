"""Helpers for audit entries written by authenticated users."""

from __future__ import annotations

from uuid import UUID

from plummy.application.security import ClientInfo, SecurityContext
from plummy.domain.audit import ActorType, AuditAction, AuditEntry, AuditOutcome


def by_user(
    context: SecurityContext,
    client: ClientInfo,
    action: AuditAction,
    *,
    target_type: str,
    target_id: UUID,
    details: dict[str, str | int | bool] | None = None,
    outcome: AuditOutcome = AuditOutcome.SUCCESS,
) -> AuditEntry:
    """``details`` must never hold secrets, tokens, passwords or plain-text emails."""
    return AuditEntry(
        action=action,
        outcome=outcome,
        actor_type=ActorType.USER,
        actor_user_id=context.user_id,
        target_type=target_type,
        target_id=target_id,
        ip_address=client.ip_address,
        user_agent=client.user_agent,
        details=details or {},
    )


def joined(values: frozenset[str] | set[str]) -> str:
    return ",".join(sorted(values))
