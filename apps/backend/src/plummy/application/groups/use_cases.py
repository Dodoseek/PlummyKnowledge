"""Groups and their members and roles (API_CONTRACTS.md §2.5).

Effective permissions include the roles of the user's groups and are recomputed on every request, so a
membership or role change applies to the members' next request. ``admin`` cannot be granted through a group.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from plummy.application.audit import by_user, joined
from plummy.application.errors import GroupNameTakenError, InvalidInputError, NotFoundError
from plummy.application.pagination import Page, PageRequest
from plummy.application.ports import Clock, UniqueViolationError, UnitOfWork, UnitOfWorkFactory
from plummy.application.security import ClientInfo, SecurityContext
from plummy.application.users.use_cases import validate_role_keys
from plummy.core.ids import new_id
from plummy.domain.audit import AuditAction
from plummy.domain.identity import (
    ROLES_NOT_ASSIGNABLE_TO_GROUPS,
    Group,
    InvalidGroupDescriptionError,
    InvalidGroupNameError,
    Permission,
    User,
    normalize_group_description,
    normalize_group_name,
)

GROUP_NOT_FOUND = "Group not found."
NAME_TAKEN = "A group with this name already exists."


@dataclass(frozen=True, slots=True)
class GroupView:
    group: Group
    member_count: int


@dataclass(frozen=True, slots=True)
class GroupDetails:
    group: Group
    members: list[User]
    roles: frozenset[str]


def _name(value: str) -> str:
    try:
        return normalize_group_name(value)
    except InvalidGroupNameError as exc:
        raise InvalidInputError("name", "invalid_group_name", str(exc)) from None


def _description(value: str | None) -> str | None:
    try:
        return normalize_group_description(value)
    except InvalidGroupDescriptionError as exc:
        raise InvalidInputError("description", "invalid_description", str(exc)) from None


async def _load(uow: UnitOfWork, group_id: UUID) -> Group:
    group = await uow.groups.get(group_id)
    if group is None:
        raise NotFoundError(GROUP_NOT_FOUND)
    return group


async def _details(uow: UnitOfWork, group: Group) -> GroupDetails:
    return GroupDetails(
        group=group,
        members=await uow.groups.members(group.id),
        roles=await uow.roles.group_role_keys(group.id),
    )


class ListGroups:
    def __init__(self, uow: UnitOfWorkFactory) -> None:
        self._uow = uow

    async def execute(
        self, context: SecurityContext, search: str | None, page: PageRequest
    ) -> Page[GroupView]:
        context.require(Permission.GROUPS_READ)
        async with self._uow() as uow:
            result = await uow.groups.page(search, page)
            counts = await uow.groups.member_counts([group.id for group in result.items])
        items = [GroupView(group=group, member_count=counts.get(group.id, 0)) for group in result.items]
        return Page(items=items, next_cursor=result.next_cursor)


class GetGroup:
    def __init__(self, uow: UnitOfWorkFactory) -> None:
        self._uow = uow

    async def execute(self, context: SecurityContext, group_id: UUID) -> GroupDetails:
        context.require(Permission.GROUPS_READ)
        async with self._uow() as uow:
            return await _details(uow, await _load(uow, group_id))


class CreateGroup:
    def __init__(self, uow: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def execute(
        self, context: SecurityContext, client: ClientInfo, name: str, description: str | None
    ) -> GroupDetails:
        context.require(Permission.GROUPS_MANAGE)
        group = Group(
            id=new_id(), name=_name(name), description=_description(description), created_at=self._clock.now()
        )
        async with self._uow() as uow:
            if await uow.groups.get_by_name(group.name) is not None:
                raise GroupNameTakenError(NAME_TAKEN)
            try:
                await uow.groups.add(group)
            except UniqueViolationError:
                raise GroupNameTakenError(NAME_TAKEN) from None
            await uow.audit.record(
                by_user(context, client, AuditAction.GROUP_CREATED, target_type="group", target_id=group.id)
            )
            await uow.commit()
        return GroupDetails(group=group, members=[], roles=frozenset())


class UpdateGroup:
    def __init__(self, uow: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def execute(
        self,
        context: SecurityContext,
        client: ClientInfo,
        group_id: UUID,
        *,
        name: str | None,
        description: str | None,
        description_set: bool,
    ) -> GroupDetails:
        """``description_set`` distinguishes "not sent" from an explicit ``null`` (clear the description)."""
        context.require(Permission.GROUPS_MANAGE)
        new_name = _name(name) if name is not None else None
        new_description = _description(description) if description_set else None
        now = self._clock.now()
        async with self._uow() as uow:
            group = await _load(uow, group_id)
            changed: list[str] = []
            if new_name is not None and new_name != group.name:
                other = await uow.groups.get_by_name(new_name)
                if other is not None and other.id != group.id:
                    raise GroupNameTakenError(NAME_TAKEN)
                group.name = new_name
                changed.append("name")
            if description_set and new_description != group.description:
                group.description = new_description
                changed.append("description")
            if changed:
                try:
                    await uow.groups.update(group, at=now)
                except UniqueViolationError:
                    raise GroupNameTakenError(NAME_TAKEN) from None
                await uow.audit.record(
                    by_user(
                        context,
                        client,
                        AuditAction.GROUP_UPDATED,
                        target_type="group",
                        target_id=group.id,
                        details={"fields": ",".join(changed)},
                    )
                )
            result = await _details(uow, group)
            await uow.commit()
        return result


class DeleteGroup:
    """Soft delete; members and roles are removed, so the members lose the group's permissions at once."""

    def __init__(self, uow: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def execute(self, context: SecurityContext, client: ClientInfo, group_id: UUID) -> None:
        context.require(Permission.GROUPS_MANAGE)
        now = self._clock.now()
        async with self._uow() as uow:
            group = await _load(uow, group_id)
            members = len(await uow.groups.members(group.id))
            roles = await uow.roles.group_role_keys(group.id)
            group.deleted_at = now
            await uow.groups.update(group, at=now)
            await uow.groups.clear(group.id)
            await uow.audit.record(
                by_user(
                    context,
                    client,
                    AuditAction.GROUP_DELETED,
                    target_type="group",
                    target_id=group.id,
                    details={"members_removed": members, "roles_removed": joined(roles)},
                )
            )
            await uow.commit()


class SetGroupMembers:
    def __init__(self, uow: UnitOfWorkFactory) -> None:
        self._uow = uow

    async def execute(
        self, context: SecurityContext, client: ClientInfo, group_id: UUID, user_ids: frozenset[UUID]
    ) -> GroupDetails:
        context.require(Permission.GROUPS_MANAGE)
        async with self._uow() as uow:
            group = await _load(uow, group_id)
            unknown = user_ids - await uow.users.existing_ids(user_ids)
            if unknown:
                raise InvalidInputError(
                    "user_ids", "unknown_user", f"{len(unknown)} user id(s) do not exist."
                )
            change = await uow.groups.set_members(group.id, user_ids)
            if change.added or change.removed:
                await uow.audit.record(
                    by_user(
                        context,
                        client,
                        AuditAction.GROUP_MEMBERS_CHANGED,
                        target_type="group",
                        target_id=group.id,
                        details={"added": len(change.added), "removed": len(change.removed)},
                    )
                )
            result = await _details(uow, group)
            await uow.commit()
        return result


class SetGroupRoles:
    def __init__(self, uow: UnitOfWorkFactory) -> None:
        self._uow = uow

    async def execute(
        self, context: SecurityContext, client: ClientInfo, group_id: UUID, roles: frozenset[str]
    ) -> GroupDetails:
        context.require(Permission.ROLES_ASSIGN)
        forbidden = roles & ROLES_NOT_ASSIGNABLE_TO_GROUPS
        if forbidden:
            raise InvalidInputError(
                "roles",
                "role_not_assignable_to_group",
                f"Role cannot be granted to a group: {', '.join(sorted(forbidden))}.",
            )
        async with self._uow() as uow:
            await validate_role_keys(uow, roles)
            group = await _load(uow, group_id)
            change = await uow.roles.set_group_roles(group.id, roles, granted_by=context.user_id)
            for role in sorted(change.added):
                await uow.audit.record(
                    by_user(
                        context,
                        client,
                        AuditAction.ROLE_ASSIGNED,
                        target_type="group",
                        target_id=group.id,
                        details={"role": role},
                    )
                )
            for role in sorted(change.removed):
                await uow.audit.record(
                    by_user(
                        context,
                        client,
                        AuditAction.ROLE_REVOKED,
                        target_type="group",
                        target_id=group.id,
                        details={"role": role},
                    )
                )
            result = await _details(uow, group)
            await uow.commit()
        return result
