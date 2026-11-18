"""Read-only role and permission catalogs (API_CONTRACTS.md §2.6). System roles cannot be changed."""

from __future__ import annotations

from plummy.application.ports import PermissionInfo, RoleInfo, UnitOfWorkFactory
from plummy.application.security import SecurityContext
from plummy.domain.identity import Permission


class ListRoles:
    def __init__(self, uow: UnitOfWorkFactory) -> None:
        self._uow = uow

    async def execute(self, context: SecurityContext) -> list[RoleInfo]:
        context.require(Permission.ROLES_READ)
        async with self._uow() as uow:
            return await uow.roles.catalog()


class ListPermissions:
    def __init__(self, uow: UnitOfWorkFactory) -> None:
        self._uow = uow

    async def execute(self, context: SecurityContext) -> list[PermissionInfo]:
        context.require(Permission.ROLES_READ)
        async with self._uow() as uow:
            return await uow.roles.permission_catalog()
