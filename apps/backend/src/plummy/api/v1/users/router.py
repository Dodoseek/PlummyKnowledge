"""``/api/v1/users`` (API_CONTRACTS.md §2.4). Thin adapters: permissions are checked by the use cases too."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status

from plummy.api.dependencies import ResourcesDep
from plummy.api.security import client_info, require_permission
from plummy.api.v1.common import allow_query, no_store, page_request
from plummy.api.v1.users.schemas import (
    ActivationLinkResponse,
    CreatedUserResponse,
    CreateUserRequest,
    RevokedSessionsResponse,
    SetRolesRequest,
    UpdateUserRequest,
    UserDetailsResponse,
    UserListResponse,
)
from plummy.application.pagination import SortSpec
from plummy.application.ports import UserQuery
from plummy.application.security import SecurityContext
from plummy.application.users.use_cases import CreateUserCommand, UpdateUserCommand
from plummy.domain.identity import Permission, UserStatus

router = APIRouter(prefix="/users", tags=["users"], dependencies=[Depends(no_store)])

SORTS = (
    SortSpec("email", "email", False, str),
    SortSpec("-email", "email", True, str),
    SortSpec("created_at", "created_at", False, datetime),
    SortSpec("-created_at", "created_at", True, datetime),
)
MAX_SEARCH_LENGTH = 200

Reader = Annotated[SecurityContext, Depends(require_permission(Permission.USERS_READ))]
Manager = Annotated[SecurityContext, Depends(require_permission(Permission.USERS_MANAGE))]
RoleAssigner = Annotated[SecurityContext, Depends(require_permission(Permission.ROLES_ASSIGN))]


@router.get(
    "",
    response_model=UserListResponse,
    dependencies=[Depends(allow_query("status", "group_id", "q", "sort", "limit", "cursor"))],
)
async def list_users(
    context: Reader,
    resources: ResourcesDep,
    status_filter: Annotated[UserStatus | None, Query(alias="status")] = None,
    group_id: UUID | None = None,
    q: Annotated[str | None, Query(max_length=MAX_SEARCH_LENGTH)] = None,
    sort: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> UserListResponse:
    page = page_request(sort, limit, cursor, SORTS, SORTS[0])
    query = UserQuery(status=status_filter, group_id=group_id, search=q.strip() if q else None)
    return UserListResponse.from_page(await resources.admin.users.list.execute(context, query, page))


@router.post("", response_model=CreatedUserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    body: CreateUserRequest, request: Request, context: Manager, resources: ResourcesDep
) -> CreatedUserResponse:
    created = await resources.admin.users.create.execute(
        context,
        client_info(request),
        CreateUserCommand(email=body.email, display_name=body.display_name, roles=frozenset(body.roles)),
    )
    return CreatedUserResponse(
        user=UserDetailsResponse.from_details(created.details),
        activation=ActivationLinkResponse.build(created.activation),
    )


@router.get("/{user_id}", response_model=UserDetailsResponse)
async def get_user(user_id: UUID, context: Reader, resources: ResourcesDep) -> UserDetailsResponse:
    return UserDetailsResponse.from_details(await resources.admin.users.get.execute(context, user_id))


@router.patch("/{user_id}", response_model=UserDetailsResponse)
async def update_user(
    user_id: UUID, body: UpdateUserRequest, request: Request, context: Manager, resources: ResourcesDep
) -> UserDetailsResponse:
    command = UpdateUserCommand(
        display_name=body.display_name, status=UserStatus(body.status) if body.status else None
    )
    details = await resources.admin.users.update.execute(context, client_info(request), user_id, command)
    return UserDetailsResponse.from_details(details)


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_user(user_id: UUID, request: Request, context: Manager, resources: ResourcesDep) -> None:
    await resources.admin.users.delete.execute(context, client_info(request), user_id)


@router.put("/{user_id}/roles", response_model=UserDetailsResponse)
async def set_user_roles(
    user_id: UUID, body: SetRolesRequest, request: Request, context: RoleAssigner, resources: ResourcesDep
) -> UserDetailsResponse:
    details = await resources.admin.users.set_roles.execute(
        context, client_info(request), user_id, frozenset(body.roles)
    )
    return UserDetailsResponse.from_details(details)


@router.post("/{user_id}/sessions/revoke", response_model=RevokedSessionsResponse)
async def revoke_user_sessions(
    user_id: UUID, request: Request, context: Manager, resources: ResourcesDep
) -> RevokedSessionsResponse:
    revoked = await resources.admin.users.revoke_sessions.execute(context, client_info(request), user_id)
    return RevokedSessionsResponse(revoked=revoked)


@router.post("/{user_id}/activation", response_model=ActivationLinkResponse)
async def reissue_activation(
    user_id: UUID, request: Request, context: Manager, resources: ResourcesDep
) -> ActivationLinkResponse:
    link = await resources.admin.users.reissue_activation.execute(context, client_info(request), user_id)
    return ActivationLinkResponse.build(link)
