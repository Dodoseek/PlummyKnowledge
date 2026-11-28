"""DTOs for ``/api/v1/audit`` (API_CONTRACTS.md §2.16, §4 ``AuditEvent``)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel

from plummy.application.pagination import Page
from plummy.domain.audit import AuditEvent


class AuditEventResponse(BaseModel):
    """As stored: events never hold secrets, tokens, passwords, plain-text emails or document content."""

    id: UUID
    created_at: datetime
    actor_type: str
    actor_user_id: UUID | None
    action: str
    target_type: str | None
    target_id: UUID | None
    outcome: str
    correlation_id: str | None
    ip_address: str | None
    user_agent: str | None
    details: dict[str, object]

    @classmethod
    def build(cls, event: AuditEvent) -> AuditEventResponse:
        return cls(
            id=event.id,
            created_at=event.created_at,
            actor_type=event.actor_type,
            actor_user_id=event.actor_user_id,
            action=event.action,
            target_type=event.target_type,
            target_id=event.target_id,
            outcome=event.outcome,
            correlation_id=event.correlation_id,
            ip_address=event.ip_address,
            user_agent=event.user_agent,
            details=event.details,
        )


class AuditEventListResponse(BaseModel):
    items: list[AuditEventResponse]
    next_cursor: str | None
    has_more: bool

    @classmethod
    def from_page(cls, page: Page[AuditEvent]) -> AuditEventListResponse:
        return cls(
            items=[AuditEventResponse.build(event) for event in page.items],
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )
