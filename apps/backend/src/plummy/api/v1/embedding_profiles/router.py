"""``/api/v1/embedding-profiles`` (API_CONTRACTS.md §2.8). The use cases check permissions too."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status

from plummy.api.dependencies import ResourcesDep
from plummy.api.security import client_info, require_permission
from plummy.api.v1.common import allow_query, no_store, page_request
from plummy.api.v1.embedding_profiles.schemas import (
    CreateEmbeddingProfileRequest,
    EmbeddingProfileListResponse,
    EmbeddingProfileResponse,
    EmbeddingProfileTestResponse,
    UpdateEmbeddingProfileRequest,
)
from plummy.application.pagination import SortSpec
from plummy.application.providers.embedding_profiles import ProfileInput, ProfilePatch
from plummy.application.security import SecurityContext
from plummy.domain.identity import Permission

router = APIRouter(
    prefix="/embedding-profiles", tags=["embedding-profiles"], dependencies=[Depends(no_store)]
)

SORTS = (
    SortSpec("name", "name", False, str),
    SortSpec("-name", "name", True, str),
    SortSpec("created_at", "created_at", False, datetime),
    SortSpec("-created_at", "created_at", True, datetime),
)

Reader = Annotated[SecurityContext, Depends(require_permission(Permission.PROVIDERS_READ))]
Manager = Annotated[SecurityContext, Depends(require_permission(Permission.PROVIDERS_MANAGE))]


@router.get(
    "",
    response_model=EmbeddingProfileListResponse,
    dependencies=[Depends(allow_query("sort", "limit", "cursor"))],
)
async def list_profiles(
    context: Reader,
    resources: ResourcesDep,
    sort: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> EmbeddingProfileListResponse:
    page = page_request(sort, limit, cursor, SORTS, SORTS[0])
    return EmbeddingProfileListResponse.from_page(
        await resources.admin.embedding_profiles.list.execute(context, page)
    )


@router.post("", response_model=EmbeddingProfileResponse, status_code=status.HTTP_201_CREATED)
async def create_profile(
    body: CreateEmbeddingProfileRequest, request: Request, context: Manager, resources: ResourcesDep
) -> EmbeddingProfileResponse:
    profile = await resources.admin.embedding_profiles.create.execute(
        context,
        client_info(request),
        ProfileInput(
            name=body.name,
            provider_id=body.provider_id,
            model_id=body.model_id,
            dimensions=body.dimensions,
            distance_metric=body.distance_metric,
            normalize=body.normalize,
            max_input_tokens=body.max_input_tokens,
        ),
    )
    return EmbeddingProfileResponse.build(profile)


@router.get("/{profile_id}", response_model=EmbeddingProfileResponse)
async def get_profile(profile_id: UUID, context: Reader, resources: ResourcesDep) -> EmbeddingProfileResponse:
    return EmbeddingProfileResponse.build(
        await resources.admin.embedding_profiles.get.execute(context, profile_id)
    )


@router.patch("/{profile_id}", response_model=EmbeddingProfileResponse)
async def update_profile(
    profile_id: UUID,
    body: UpdateEmbeddingProfileRequest,
    request: Request,
    context: Manager,
    resources: ResourcesDep,
) -> EmbeddingProfileResponse:
    profile = await resources.admin.embedding_profiles.update.execute(
        context,
        client_info(request),
        profile_id,
        ProfilePatch(
            name=body.name,
            status=body.status,
            provider_id=body.provider_id,
            model_id=body.model_id,
            dimensions=body.dimensions,
            distance_metric=body.distance_metric,
            normalize=body.normalize,
        ),
    )
    return EmbeddingProfileResponse.build(profile)


@router.post("/{profile_id}/test", response_model=EmbeddingProfileTestResponse)
async def test_profile(
    profile_id: UUID, context: Manager, resources: ResourcesDep
) -> EmbeddingProfileTestResponse:
    report = await resources.admin.embedding_profiles.test.execute(context, profile_id)
    return EmbeddingProfileTestResponse.build(report)
