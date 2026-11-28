"""``/api/v1/data-sources`` (API_CONTRACTS.md §2.9, ADR-020). Thin adapters: the use cases check permissions
and knowledge-base access too."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import RedirectResponse

from plummy.api.dependencies import ResourcesDep
from plummy.api.security import (
    SettingsDep,
    client_info,
    limit_by_address,
    read_session_token,
    require_permission,
)
from plummy.api.v1.common import allow_query, no_store, page_request
from plummy.api.v1.data_sources.schemas import (
    BrowseResponse,
    CreateDataSourceRequest,
    DataSourceListResponse,
    DataSourceResponse,
    DeletionResponse,
    OAuthStartResponse,
    SyncJobResponse,
    SyncRequest,
    UpdateDataSourceRequest,
    ValidationResponse,
)
from plummy.application.errors import AuthenticationRequiredError
from plummy.application.pagination import SortSpec
from plummy.application.security import SecurityContext
from plummy.application.sources.use_cases import CallbackParams, DataSourceInput, DataSourcePatch
from plummy.domain.identity import Permission

router = APIRouter(prefix="/data-sources", tags=["data-sources"], dependencies=[Depends(no_store)])

SORTS = (
    SortSpec("created_at", "created_at", False, datetime),
    SortSpec("-created_at", "created_at", True, datetime),
    SortSpec("name", "name", False, str),
    SortSpec("-name", "name", True, str),
)
MAX_CALLBACK_PARAM_LENGTH = 4096

Reader = Annotated[SecurityContext, Depends(require_permission(Permission.DATA_SOURCES_READ))]
Manager = Annotated[SecurityContext, Depends(require_permission(Permission.DATA_SOURCES_MANAGE))]
Syncer = Annotated[SecurityContext, Depends(require_permission(Permission.DATA_SOURCES_SYNC))]


@router.get(
    "",
    response_model=DataSourceListResponse,
    dependencies=[Depends(allow_query("knowledge_base_id", "sort", "limit", "cursor"))],
)
async def list_data_sources(
    context: Reader,
    resources: ResourcesDep,
    knowledge_base_id: UUID | None = None,
    sort: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> DataSourceListResponse:
    page = page_request(sort, limit, cursor, SORTS, SORTS[0])
    result = await resources.sources.list.execute(context, page, knowledge_base_id=knowledge_base_id)
    return DataSourceListResponse.from_page(result)


@router.post("", response_model=DataSourceResponse, status_code=status.HTTP_201_CREATED)
async def create_data_source(
    body: CreateDataSourceRequest, request: Request, context: Manager, resources: ResourcesDep
) -> DataSourceResponse:
    view = await resources.sources.create.execute(
        context,
        client_info(request),
        DataSourceInput(
            type=body.type,
            name=body.name,
            knowledge_base_id=body.knowledge_base_id,
            sync_interval_minutes=body.sync_interval_minutes,
        ),
    )
    return DataSourceResponse.build(view)


def _bounded(value: str | None) -> str | None:
    return value if value is not None and len(value) <= MAX_CALLBACK_PARAM_LENGTH else None


@router.get(
    "/oauth/callback",
    response_class=RedirectResponse,
    status_code=status.HTTP_303_SEE_OTHER,
    dependencies=[Depends(limit_by_address)],
)
async def oauth_callback(
    request: Request,
    resources: ResourcesDep,
    settings: SettingsDep,
    state: str | None = None,
    code: str | None = None,
    error: str | None = None,
) -> RedirectResponse:
    """Public: the browser arrives from the provider. The pending authorization behind ``state`` must match
    the current session; the answer always redirects to a UI path built by the server."""
    context: SecurityContext | None = None
    token = read_session_token(request, settings)
    if token is not None:
        try:
            context = await resources.auth.authenticate.execute(token)
        except AuthenticationRequiredError:
            context = None
    location = await resources.sources.complete_oauth.execute(
        context,
        client_info(request),
        CallbackParams(state=_bounded(state), code=_bounded(code), error=_bounded(error)),
    )
    response = RedirectResponse(location, status_code=status.HTTP_303_SEE_OTHER)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"  # the URL carried the authorization code
    return response


@router.get("/{source_id}", response_model=DataSourceResponse)
async def get_data_source(source_id: UUID, context: Reader, resources: ResourcesDep) -> DataSourceResponse:
    return DataSourceResponse.build(await resources.sources.get.execute(context, source_id))


@router.patch("/{source_id}", response_model=DataSourceResponse)
async def update_data_source(
    source_id: UUID,
    body: UpdateDataSourceRequest,
    request: Request,
    context: Manager,
    resources: ResourcesDep,
) -> DataSourceResponse:
    view = await resources.sources.update.execute(
        context, client_info(request), source_id, DataSourcePatch(**body.patch_values())
    )
    return DataSourceResponse.build(view)


@router.delete("/{source_id}", response_model=DeletionResponse)
async def delete_data_source(
    source_id: UUID, request: Request, context: Manager, resources: ResourcesDep
) -> DeletionResponse:
    """Its documents leave search at once; their files, chunks and vectors are purged in the background."""
    deleted = await resources.sources.delete.execute(context, client_info(request), source_id)
    return DeletionResponse(documents_deleted=deleted)


@router.post("/{source_id}/oauth/start", response_model=OAuthStartResponse)
async def start_oauth(
    source_id: UUID, request: Request, context: Manager, resources: ResourcesDep
) -> OAuthStartResponse:
    url = await resources.sources.start_oauth.execute(context, client_info(request), source_id)
    return OAuthStartResponse(authorization_url=url)


@router.post("/{source_id}/disconnect", response_model=DataSourceResponse)
async def disconnect_data_source(
    source_id: UUID, request: Request, context: Manager, resources: ResourcesDep
) -> DataSourceResponse:
    return DataSourceResponse.build(
        await resources.sources.disconnect.execute(context, client_info(request), source_id)
    )


@router.post("/{source_id}/sync", response_model=SyncJobResponse, status_code=status.HTTP_202_ACCEPTED)
async def sync_data_source(
    source_id: UUID,
    request: Request,
    context: Syncer,
    resources: ResourcesDep,
    body: SyncRequest | None = None,
) -> SyncJobResponse:
    """Queues a sync job (``409 sync_in_progress`` while one is queued or running); the worker runs it."""
    mode = (body or SyncRequest()).sync_mode()
    job = await resources.sources.sync.execute(context, client_info(request), source_id, mode)
    return SyncJobResponse.build(job)


@router.post("/{source_id}/validate", response_model=ValidationResponse)
async def validate_data_source(
    source_id: UUID, context: Manager, resources: ResourcesDep
) -> ValidationResponse:
    """A fresh token, the account and the chosen folder; provider failures come back as ``failed``."""
    return ValidationResponse.build(await resources.sources.validate.execute(context, source_id))


@router.get(
    "/{source_id}/browse",
    response_model=BrowseResponse,
    dependencies=[Depends(allow_query("parent_id", "cursor"))],
)
async def browse_data_source(
    source_id: UUID,
    context: Manager,
    resources: ResourcesDep,
    parent_id: str | None = None,
    cursor: str | None = None,
) -> BrowseResponse:
    """Folders for the picker: the top level without ``parent_id``."""
    page = await resources.sources.browse.execute(context, source_id, parent_id, cursor)
    return BrowseResponse.build(page)
