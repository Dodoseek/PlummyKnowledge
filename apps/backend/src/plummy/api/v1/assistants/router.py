"""``/api/v1/assistants`` (API_CONTRACTS.md §2.12, ADR-005). Access is decided by the use cases from the
verified session on every request; the router only requires a signed-in user."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status

from plummy.api.dependencies import ResourcesDep
from plummy.api.security import CurrentContext, client_info
from plummy.api.v1.assistants.schemas import (
    AssistantBody,
    AssistantListResponse,
    AssistantResponse,
    CreateAssistantBody,
)
from plummy.api.v1.common import allow_query, no_store, page_request
from plummy.application.pagination import SortSpec

router = APIRouter(prefix="/assistants", tags=["assistants"], dependencies=[Depends(no_store)])

SORTS = (
    SortSpec("name", "name", False, str),
    SortSpec("-name", "name", True, str),
    SortSpec("created_at", "created_at", False, datetime),
    SortSpec("-created_at", "created_at", True, datetime),
)


@router.get(
    "", response_model=AssistantListResponse, dependencies=[Depends(allow_query("sort", "limit", "cursor"))]
)
async def list_assistants(
    context: CurrentContext,
    resources: ResourcesDep,
    sort: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> AssistantListResponse:
    page = page_request(sort, limit, cursor, SORTS, SORTS[0])
    return AssistantListResponse.from_page(await resources.assistants.list.execute(context, page))


@router.post("", response_model=AssistantResponse, status_code=status.HTTP_201_CREATED)
async def create_assistant(
    body: CreateAssistantBody, request: Request, context: CurrentContext, resources: ResourcesDep
) -> AssistantResponse:
    view = await resources.assistants.create.execute(context, client_info(request), body.changes())
    return AssistantResponse.build(view)


@router.get("/{assistant_id}", response_model=AssistantResponse)
async def get_assistant(
    assistant_id: UUID, context: CurrentContext, resources: ResourcesDep
) -> AssistantResponse:
    return AssistantResponse.build(await resources.assistants.get.execute(context, assistant_id))


@router.patch("/{assistant_id}", response_model=AssistantResponse)
async def update_assistant(
    assistant_id: UUID,
    body: AssistantBody,
    request: Request,
    context: CurrentContext,
    resources: ResourcesDep,
) -> AssistantResponse:
    view = await resources.assistants.update.execute(
        context, client_info(request), assistant_id, body.changes()
    )
    return AssistantResponse.build(view)


@router.delete("/{assistant_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_assistant(
    assistant_id: UUID, request: Request, context: CurrentContext, resources: ResourcesDep
) -> Response:
    await resources.assistants.delete.execute(context, client_info(request), assistant_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
