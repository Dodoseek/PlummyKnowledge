"""DTOs for ``/api/v1/groups`` (API_CONTRACTS.md §2.5)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from plummy.application.groups.use_cases import GroupDetails, GroupView
from plummy.application.pagination import Page
from plummy.domain.identity import User

MAX_ROLES = 20
MAX_MEMBERS = 1000
MAX_NAME_INPUT_LENGTH = 200
MAX_DESCRIPTION_INPUT_LENGTH = 1000


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GroupResponse(BaseModel):
    id: UUID
    name: str
    description: str | None
    member_count: int
    created_at: datetime

    @classmethod
    def from_view(cls, view: GroupView) -> GroupResponse:
        group = view.group
        return cls(
            id=group.id,
            name=group.name,
            description=group.description,
            member_count=view.member_count,
            created_at=group.created_at,
        )


class GroupMember(BaseModel):
    id: UUID
    email: str
    display_name: str
    status: str

    @classmethod
    def build(cls, user: User) -> GroupMember:
        return cls(id=user.id, email=user.email, display_name=user.display_name, status=user.status.value)


class GroupDetailsResponse(GroupResponse):
    members: list[GroupMember]
    roles: list[str]

    @classmethod
    def from_details(cls, details: GroupDetails) -> GroupDetailsResponse:
        group = details.group
        return cls(
            id=group.id,
            name=group.name,
            description=group.description,
            member_count=len(details.members),
            created_at=group.created_at,
            members=[GroupMember.build(user) for user in details.members],
            roles=sorted(details.roles),
        )


class GroupListResponse(BaseModel):
    items: list[GroupResponse]
    next_cursor: str | None
    has_more: bool

    @classmethod
    def from_page(cls, page: Page[GroupView]) -> GroupListResponse:
        return cls(
            items=[GroupResponse.from_view(view) for view in page.items],
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )


class CreateGroupRequest(_Request):
    name: str = Field(min_length=1, max_length=MAX_NAME_INPUT_LENGTH)
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION_INPUT_LENGTH)


class UpdateGroupRequest(_Request):
    """``description: null`` clears the description; an omitted field is left unchanged."""

    name: str | None = Field(default=None, min_length=1, max_length=MAX_NAME_INPUT_LENGTH)
    description: str | None = Field(default=None, max_length=MAX_DESCRIPTION_INPUT_LENGTH)


class SetMembersRequest(_Request):
    user_ids: list[UUID] = Field(max_length=MAX_MEMBERS)


class SetGroupRolesRequest(_Request):
    roles: list[str] = Field(max_length=MAX_ROLES)
