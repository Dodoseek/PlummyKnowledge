"""``/api/v1/audit`` (API_CONTRACTS.md §2.16): the audit log, read only, with ``audit.read``.

There are no routes to change or delete events. Reading the log is not itself audited (``audit.viewed``
is [Proposed]).
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import AwareDatetime

from plummy.api.dependencies import ResourcesDep
from plummy.api.security import require_permission
from plummy.api.v1.audit.schemas import AuditEventListResponse, AuditEventResponse
from plummy.api.v1.common import allow_query, no_store, page_request
from plummy.application.pagination import SortSpec
from plummy.application.ports import AuditQuery
from plummy.application.security import SecurityContext
from plummy.domain.audit import AuditOutcome
from plummy.domain.identity import Permission

router = APIRouter(prefix="/audit", tags=["audit"], dependencies=[Depends(no_store)])

SORTS = (
    SortSpec("-created_at", "created_at", True, datetime),
    SortSpec("created_at", "created_at", False, datetime),
)
FILTERS = ("action", "actor_user_id", "target_type", "target_id", "outcome", "from", "to")

Reader = Annotated[SecurityContext, Depends(require_permission(Permission.AUDIT_READ))]
Name = Annotated[str | None, Query(min_length=1, max_length=128)]


@router.get(
    "/events",
    response_model=AuditEventListResponse,
    dependencies=[Depends(allow_query(*FILTERS, "sort", "limit", "cursor"))],
)
async def list_events(
    context: Reader,
    resources: ResourcesDep,
    action: Name = None,
    actor_user_id: UUID | None = None,
    target_type: Name = None,
    target_id: UUID | None = None,
    outcome: AuditOutcome | None = None,
    created_from: Annotated[AwareDatetime | None, Query(alias="from")] = None,
    created_to: Annotated[AwareDatetime | None, Query(alias="to")] = None,
    sort: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> AuditEventListResponse:
    page = page_request(sort, limit, cursor, SORTS, SORTS[0])
    query = AuditQuery(
        action=action,
        actor_user_id=actor_user_id,
        target_type=target_type,
        target_id=target_id,
        outcome=outcome.value if outcome is not None else None,
        created_from=created_from,
        created_to=created_to,
    )
    return AuditEventListResponse.from_page(await resources.audit.list.execute(context, query, page))


@router.get("/events/{event_id}", response_model=AuditEventResponse)
async def get_event(event_id: UUID, context: Reader, resources: ResourcesDep) -> AuditEventResponse:
    return AuditEventResponse.build(await resources.audit.get.execute(context, event_id))
