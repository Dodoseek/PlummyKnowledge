"""``/api/v1/knowledge-bases`` (API_CONTRACTS.md §2.10). Access to each knowledge base is checked by the use
cases from the verified session; the router only requires a signed-in user."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status

from plummy.api.dependencies import ResourcesDep
from plummy.api.security import CurrentContext, client_info, require_permission
from plummy.api.v1.common import allow_query, no_store, page_request
from plummy.api.v1.knowledge_bases.schemas import (
    CreateKnowledgeBaseRequest,
    KnowledgeBaseListResponse,
    KnowledgeBaseResponse,
)
from plummy.application.knowledge.use_cases import KnowledgeBaseInput
from plummy.application.pagination import SortSpec
from plummy.application.security import SecurityContext
from plummy.domain.identity import Permission

router = APIRouter(prefix="/knowledge-bases", tags=["knowledge-bases"], dependencies=[Depends(no_store)])

SORTS = (
    SortSpec("name", "name", False, str),
    SortSpec("-name", "name", True, str),
    SortSpec("created_at", "created_at", False, datetime),
    SortSpec("-created_at", "created_at", True, datetime),
)

Manager = Annotated[SecurityContext, Depends(require_permission(Permission.KNOWLEDGE_BASES_MANAGE))]


@router.get(
    "",
    response_model=KnowledgeBaseListResponse,
    dependencies=[Depends(allow_query("sort", "limit", "cursor"))],
)
async def list_knowledge_bases(
    context: CurrentContext,
    resources: ResourcesDep,
    sort: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> KnowledgeBaseListResponse:
    page = page_request(sort, limit, cursor, SORTS, SORTS[0])
    return KnowledgeBaseListResponse.from_page(await resources.knowledge.list.execute(context, page))


@router.post("", response_model=KnowledgeBaseResponse, status_code=status.HTTP_201_CREATED)
async def create_knowledge_base(
    body: CreateKnowledgeBaseRequest, request: Request, context: Manager, resources: ResourcesDep
) -> KnowledgeBaseResponse:
    view = await resources.knowledge.create.execute(
        context,
        client_info(request),
        KnowledgeBaseInput(
            name=body.name, description=body.description, embedding_profile_id=body.embedding_profile_id
        ),
    )
    return KnowledgeBaseResponse.build(view)


@router.get("/{kb_id}", response_model=KnowledgeBaseResponse)
async def get_knowledge_base(
    kb_id: UUID, context: CurrentContext, resources: ResourcesDep
) -> KnowledgeBaseResponse:
    return KnowledgeBaseResponse.build(await resources.knowledge.get.execute(context, kb_id))
