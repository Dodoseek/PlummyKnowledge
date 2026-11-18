"""``/api/v1/roles`` (API_CONTRACTS.md §2.6): read-only catalogs of system roles and permissions."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from plummy.api.dependencies import ResourcesDep
from plummy.api.security import require_permission
from plummy.application.security import SecurityContext
from plummy.domain.identity import Permission

router = APIRouter(prefix="/roles", tags=["roles"])

Reader = Annotated[SecurityContext, Depends(require_permission(Permission.ROLES_READ))]


class RoleResponse(BaseModel):
    key: str
    name: str
    description: str
    is_system: bool
    permissions: list[str]


class RoleListResponse(BaseModel):
    items: list[RoleResponse]


class PermissionResponse(BaseModel):
    key: str
    description: str


class PermissionListResponse(BaseModel):
    items: list[PermissionResponse]


@router.get("", response_model=RoleListResponse)
async def list_roles(context: Reader, resources: ResourcesDep) -> RoleListResponse:
    roles = await resources.admin.roles.execute(context)
    return RoleListResponse(
        items=[
            RoleResponse(
                key=role.key,
                name=role.name,
                description=role.description,
                is_system=role.is_system,
                permissions=list(role.permissions),
            )
            for role in roles
        ]
    )


@router.get("/permissions", response_model=PermissionListResponse)
async def list_permissions(context: Reader, resources: ResourcesDep) -> PermissionListResponse:
    permissions = await resources.admin.permissions.execute(context)
    return PermissionListResponse(
        items=[PermissionResponse(key=item.key, description=item.description) for item in permissions]
    )
