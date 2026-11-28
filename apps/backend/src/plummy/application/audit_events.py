"""Reading the audit log (API_CONTRACTS.md §2.16): ``audit.read`` only, and read only.

The log is append-only (ADR-024): there is no use case to change or delete an event; retention is a
maintenance command under the schema owner role. Events never hold secrets, tokens, passwords, plain-text
emails or document content (:class:`~plummy.domain.audit.AuditEntry`), so they are returned as stored.
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

from plummy.application.errors import InvalidQueryParameterError, NotFoundError
from plummy.application.pagination import Page, PageRequest
from plummy.application.ports import AuditQuery, UnitOfWorkFactory
from plummy.application.security import SecurityContext
from plummy.domain.audit import AuditEvent
from plummy.domain.identity import Permission

EVENT_NOT_FOUND: Final = "Audit event not found."


class ListAuditEvents:
    def __init__(self, uow: UnitOfWorkFactory) -> None:
        self._uow = uow

    async def execute(
        self, context: SecurityContext, query: AuditQuery, page: PageRequest
    ) -> Page[AuditEvent]:
        context.require(Permission.AUDIT_READ)
        if (
            query.created_from is not None
            and query.created_to is not None
            and query.created_from >= query.created_to
        ):
            raise InvalidQueryParameterError("to", "invalid_range", "'to' must be later than 'from'.")
        async with self._uow() as uow:
            return await uow.audit.page(query, page)


class GetAuditEvent:
    def __init__(self, uow: UnitOfWorkFactory) -> None:
        self._uow = uow

    async def execute(self, context: SecurityContext, event_id: UUID) -> AuditEvent:
        context.require(Permission.AUDIT_READ)
        async with self._uow() as uow:
            event = await uow.audit.get(event_id)
        if event is None:
            raise NotFoundError(EVENT_NOT_FOUND)
        return event
