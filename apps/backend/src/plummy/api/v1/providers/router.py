"""``/api/v1/providers`` (API_CONTRACTS.md §2.7). Thin adapters: the use cases check permissions too."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status

from plummy.api.dependencies import ResourcesDep
from plummy.api.security import client_info, require_permission
from plummy.api.v1.common import allow_query, no_store, page_request
from plummy.api.v1.providers.schemas import (
    ConnectionTestResponse,
    CreateModelRequest,
    CreateProviderRequest,
    ModelListResponse,
    ModelResponse,
    ProviderListResponse,
    ProviderResponse,
    UpdateModelRequest,
    UpdateProviderRequest,
)
from plummy.application.pagination import SortSpec
from plummy.application.providers.use_cases import ModelInput, ModelPatch, ProviderInput, ProviderPatch
from plummy.application.security import SecurityContext
from plummy.domain.identity import Permission

router = APIRouter(prefix="/providers", tags=["providers"], dependencies=[Depends(no_store)])

SORTS = (
    SortSpec("name", "name", False, str),
    SortSpec("-name", "name", True, str),
    SortSpec("created_at", "created_at", False, datetime),
    SortSpec("-created_at", "created_at", True, datetime),
)

Reader = Annotated[SecurityContext, Depends(require_permission(Permission.PROVIDERS_READ))]
Manager = Annotated[SecurityContext, Depends(require_permission(Permission.PROVIDERS_MANAGE))]


@router.get(
    "", response_model=ProviderListResponse, dependencies=[Depends(allow_query("sort", "limit", "cursor"))]
)
async def list_providers(
    context: Reader,
    resources: ResourcesDep,
    sort: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> ProviderListResponse:
    page = page_request(sort, limit, cursor, SORTS, SORTS[0])
    return ProviderListResponse.from_page(await resources.admin.providers.list.execute(context, page))


@router.post("", response_model=ProviderResponse, status_code=status.HTTP_201_CREATED)
async def create_provider(
    body: CreateProviderRequest, request: Request, context: Manager, resources: ResourcesDep
) -> ProviderResponse:
    provider = await resources.admin.providers.create.execute(
        context,
        client_info(request),
        ProviderInput(
            name=body.name,
            provider_type=body.provider_type,
            base_url=body.base_url,
            api_key=body.api_key.get_secret_value() if body.api_key is not None else None,
            timeout_seconds=body.settings.timeout_seconds if body.settings else None,
            enabled=body.enabled,
        ),
    )
    return ProviderResponse.build(provider)


@router.get("/{provider_id}", response_model=ProviderResponse)
async def get_provider(provider_id: UUID, context: Reader, resources: ResourcesDep) -> ProviderResponse:
    return ProviderResponse.build(await resources.admin.providers.get.execute(context, provider_id))


@router.patch("/{provider_id}", response_model=ProviderResponse)
async def update_provider(
    provider_id: UUID,
    body: UpdateProviderRequest,
    request: Request,
    context: Manager,
    resources: ResourcesDep,
) -> ProviderResponse:
    provider = await resources.admin.providers.update.execute(
        context,
        client_info(request),
        provider_id,
        ProviderPatch(
            name=body.name,
            base_url=body.base_url,
            timeout_seconds=body.settings.timeout_seconds if body.settings else None,
            enabled=body.enabled,
            api_key=body.api_key.get_secret_value() if body.api_key is not None else None,
            api_key_set="api_key" in body.model_fields_set,
        ),
    )
    return ProviderResponse.build(provider)


@router.delete("/{provider_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_provider(
    provider_id: UUID, request: Request, context: Manager, resources: ResourcesDep
) -> None:
    await resources.admin.providers.delete.execute(context, client_info(request), provider_id)


@router.post("/{provider_id}/test", response_model=ConnectionTestResponse)
async def test_provider(
    provider_id: UUID, request: Request, context: Manager, resources: ResourcesDep
) -> ConnectionTestResponse:
    report = await resources.admin.providers.test.execute(context, client_info(request), provider_id)
    return ConnectionTestResponse.build(report)


@router.get("/{provider_id}/models", response_model=ModelListResponse)
async def list_models(provider_id: UUID, context: Reader, resources: ResourcesDep) -> ModelListResponse:
    models = await resources.admin.providers.list_models.execute(context, provider_id)
    return ModelListResponse(items=[ModelResponse.build(model) for model in models])


@router.post("/{provider_id}/models", response_model=ModelResponse, status_code=status.HTTP_201_CREATED)
async def create_model(
    provider_id: UUID, body: CreateModelRequest, request: Request, context: Manager, resources: ResourcesDep
) -> ModelResponse:
    model = await resources.admin.providers.create_model.execute(
        context,
        client_info(request),
        provider_id,
        ModelInput(
            model_id=body.model_id,
            display_name=body.display_name,
            context_window=body.context_window,
            max_output_tokens=body.max_output_tokens,
            enabled=body.enabled,
        ),
    )
    return ModelResponse.build(model)


@router.patch("/{provider_id}/models/{config_id}", response_model=ModelResponse)
async def update_model(
    provider_id: UUID,
    config_id: UUID,
    body: UpdateModelRequest,
    request: Request,
    context: Manager,
    resources: ResourcesDep,
) -> ModelResponse:
    sent = body.model_fields_set
    model = await resources.admin.providers.update_model.execute(
        context,
        client_info(request),
        provider_id,
        config_id,
        ModelPatch(
            display_name=body.display_name,
            display_name_set="display_name" in sent,
            context_window=body.context_window,
            context_window_set="context_window" in sent,
            max_output_tokens=body.max_output_tokens,
            max_output_tokens_set="max_output_tokens" in sent,
            enabled=body.enabled,
        ),
    )
    return ModelResponse.build(model)


@router.delete(
    "/{provider_id}/models/{config_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response
)
async def delete_model(
    provider_id: UUID, config_id: UUID, request: Request, context: Manager, resources: ResourcesDep
) -> None:
    await resources.admin.providers.delete_model.execute(
        context, client_info(request), provider_id, config_id
    )
