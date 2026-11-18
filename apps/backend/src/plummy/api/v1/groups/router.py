"""``/api/v1/groups`` (API_CONTRACTS.md §2.5). Thin adapters: permissions are checked by the use cases too."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status

from plummy.api.dependencies import ResourcesDep
from plummy.api.security import client_info, require_permission
from plummy.api.v1.common import allow_query, no_store, page_request
from plummy.api.v1.groups.schemas import (
    CreateGroupRequest,
    GroupDetailsResponse,
    GroupListResponse,
    SetGroupRolesRequest,
    SetMembersRequest,
    UpdateGroupRequest,
)
from plummy.application.pagination import SortSpec
from plummy.application.security import SecurityContext
from plummy.domain.identity import Permission

router = APIRouter(prefix="/groups", tags=["groups"], dependencies=[Depends(no_store)])

SORTS = (
    SortSpec("name", "name", False, str),
    SortSpec("-name", "name", True, str),
    SortSpec("created_at", "created_at", False, datetime),
    SortSpec("-created_at", "created_at", True, datetime),
)
MAX_SEARCH_LENGTH = 200

Reader = Annotated[SecurityContext, Depends(require_permission(Permission.GROUPS_READ))]
Manager = Annotated[SecurityContext, Depends(require_permission(Permission.GROUPS_MANAGE))]
RoleAssigner = Annotated[SecurityContext, Depends(require_permission(Permission.ROLES_ASSIGN))]


@router.get(
    "", response_model=GroupListResponse, dependencies=[Depends(allow_query("q", "sort", "limit", "cursor"))]
)
async def list_groups(
    context: Reader,
    resources: ResourcesDep,
    q: Annotated[str | None, Query(max_length=MAX_SEARCH_LENGTH)] = None,
    sort: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> GroupListResponse:
    page = page_request(sort, limit, cursor, SORTS, SORTS[0])
    search = q.strip() if q else None
    return GroupListResponse.from_page(
        await resources.admin.groups.list.execute(context, search or None, page)
    )


@router.post("", response_model=GroupDetailsResponse, status_code=status.HTTP_201_CREATED)
async def create_group(
    body: CreateGroupRequest, request: Request, context: Manager, resources: ResourcesDep
) -> GroupDetailsResponse:
    details = await resources.admin.groups.create.execute(
        context, client_info(request), body.name, body.description
    )
    return GroupDetailsResponse.from_details(details)


@router.get("/{group_id}", response_model=GroupDetailsResponse)
async def get_group(group_id: UUID, context: Reader, resources: ResourcesDep) -> GroupDetailsResponse:
    return GroupDetailsResponse.from_details(await resources.admin.groups.get.execute(context, group_id))


@router.patch("/{group_id}", response_model=GroupDetailsResponse)
async def update_group(
    group_id: UUID, body: UpdateGroupRequest, request: Request, context: Manager, resources: ResourcesDep
) -> GroupDetailsResponse:
    details = await resources.admin.groups.update.execute(
        context,
        client_info(request),
        group_id,
        name=body.name,
        description=body.description,
        description_set="description" in body.model_fields_set,
    )
    return GroupDetailsResponse.from_details(details)


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_group(group_id: UUID, request: Request, context: Manager, resources: ResourcesDep) -> None:
    await resources.admin.groups.delete.execute(context, client_info(request), group_id)


@router.put("/{group_id}/members", response_model=GroupDetailsResponse)
async def set_group_members(
    group_id: UUID, body: SetMembersRequest, request: Request, context: Manager, resources: ResourcesDep
) -> GroupDetailsResponse:
    details = await resources.admin.groups.set_members.execute(
        context, client_info(request), group_id, frozenset(body.user_ids)
    )
    return GroupDetailsResponse.from_details(details)


@router.put("/{group_id}/roles", response_model=GroupDetailsResponse)
async def set_group_roles(
    group_id: UUID,
    body: SetGroupRolesRequest,
    request: Request,
    context: RoleAssigner,
    resources: ResourcesDep,
) -> GroupDetailsResponse:
    details = await resources.admin.groups.set_roles.execute(
        context, client_info(request), group_id, frozenset(body.roles)
    )
    return GroupDetailsResponse.from_details(details)
