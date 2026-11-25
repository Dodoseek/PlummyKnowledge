"""``/api/v1/knowledge-bases`` (API_CONTRACTS.md §2.10). Access to each knowledge base is checked by the use
cases from the verified session; the router only requires a signed-in user."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status

from plummy.api.dependencies import ResourcesDep
from plummy.api.security import CurrentContext, client_info, require_permission
from plummy.api.v1.common import allow_query, no_store, page_request
from plummy.api.v1.knowledge_bases.schemas import (
    AccessResponse,
    CreateKnowledgeBaseRequest,
    KnowledgeBaseListResponse,
    KnowledgeBaseReindexResponse,
    KnowledgeBaseResponse,
    ReplaceAccessRequest,
    SearchRequest,
    SearchResponse,
    UpdateKnowledgeBaseRequest,
)
from plummy.application.knowledge.use_cases import GrantInput, KnowledgeBaseChanges, KnowledgeBaseInput
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


@router.patch("/{kb_id}", response_model=KnowledgeBaseResponse)
async def update_knowledge_base(
    kb_id: UUID,
    body: UpdateKnowledgeBaseRequest,
    request: Request,
    context: CurrentContext,
    resources: ResourcesDep,
) -> KnowledgeBaseResponse:
    sent = body.model_fields_set
    changes = KnowledgeBaseChanges(
        name=body.name,
        description=body.description,
        set_description="description" in sent,
        embedding_profile_id=body.embedding_profile_id,
    )
    view = await resources.knowledge.update.execute(context, client_info(request), kb_id, changes)
    return KnowledgeBaseResponse.build(view)


@router.delete("/{kb_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_knowledge_base(
    kb_id: UUID, request: Request, context: Manager, resources: ResourcesDep
) -> Response:
    """Its documents leave search at once; their files, chunks and vectors are purged in the background."""
    await resources.knowledge.delete.execute(context, client_info(request), kb_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{kb_id}/access", response_model=AccessResponse)
async def get_knowledge_base_access(
    kb_id: UUID, context: CurrentContext, resources: ResourcesDep
) -> AccessResponse:
    return AccessResponse.build(await resources.knowledge.access.execute(context, kb_id))


@router.put("/{kb_id}/access", response_model=AccessResponse)
async def replace_knowledge_base_access(
    kb_id: UUID,
    body: ReplaceAccessRequest,
    request: Request,
    context: CurrentContext,
    resources: ResourcesDep,
) -> AccessResponse:
    entries = [GrantInput(g.principal_type, g.principal_id, g.access_level) for g in body.grants]
    views = await resources.knowledge.replace_access.execute(context, client_info(request), kb_id, entries)
    return AccessResponse.build(views)


@router.post(
    "/{kb_id}/reindex", response_model=KnowledgeBaseReindexResponse, status_code=status.HTTP_202_ACCEPTED
)
async def reindex_knowledge_base(
    kb_id: UUID, request: Request, context: CurrentContext, resources: ResourcesDep
) -> KnowledgeBaseReindexResponse:
    result = await resources.documents.reindex_knowledge_base.execute(context, client_info(request), kb_id)
    return KnowledgeBaseReindexResponse.build(result)


@router.post("/{kb_id}/search", response_model=SearchResponse)
async def search_knowledge_base(
    kb_id: UUID, body: SearchRequest, context: CurrentContext, resources: ResourcesDep
) -> SearchResponse:
    result = await resources.knowledge.search.execute(
        context, kb_id, body.query, mode=body.mode, top_k=body.top_k
    )
    return SearchResponse.build(result)
