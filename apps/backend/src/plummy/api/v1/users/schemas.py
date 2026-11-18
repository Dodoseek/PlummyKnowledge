"""DTOs for ``/api/v1/users`` (API_CONTRACTS.md §2.4). Passwords and hashes are never part of a response."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from plummy.api.v1.auth.schemas import MAX_EMAIL_INPUT_LENGTH
from plummy.application.pagination import Page
from plummy.application.users.use_cases import ActivationLink, UserDetails, UserView
from plummy.domain.identity import Group, User

MAX_ROLES = 20
MAX_NAME_INPUT_LENGTH = 400


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GroupRef(BaseModel):
    id: UUID
    name: str

    @classmethod
    def build(cls, group: Group) -> GroupRef:
        return cls(id=group.id, name=group.name)


class UserResponse(BaseModel):
    id: UUID
    email: str
    display_name: str
    status: str
    roles: list[str]
    created_at: datetime
    last_login_at: datetime | None

    @classmethod
    def build(cls, user: User, roles: frozenset[str]) -> UserResponse:
        return cls(
            id=user.id,
            email=user.email,
            display_name=user.display_name,
            status=user.status.value,
            roles=sorted(roles),
            created_at=user.created_at,
            last_login_at=user.last_login_at,
        )

    @classmethod
    def from_view(cls, view: UserView) -> UserResponse:
        return cls.build(view.user, view.roles)


class UserDetailsResponse(UserResponse):
    """``roles`` are the direct roles; roles inherited through ``groups`` are shown on each group."""

    groups: list[GroupRef]

    @classmethod
    def from_details(cls, details: UserDetails) -> UserDetailsResponse:
        base = UserResponse.build(details.user, details.roles)
        return cls(**base.model_dump(), groups=[GroupRef.build(group) for group in details.groups])


class UserListResponse(BaseModel):
    items: list[UserResponse]
    next_cursor: str | None
    has_more: bool

    @classmethod
    def from_page(cls, page: Page[UserView]) -> UserListResponse:
        return cls(
            items=[UserResponse.from_view(view) for view in page.items],
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )


class ActivationLinkResponse(BaseModel):
    """Shown once. The link grants access to the account until it is used or expires."""

    url: str
    expires_at: datetime

    @classmethod
    def build(cls, link: ActivationLink) -> ActivationLinkResponse:
        return cls(url=link.url, expires_at=link.expires_at)


class CreateUserRequest(_Request):
    email: str = Field(min_length=1, max_length=MAX_EMAIL_INPUT_LENGTH)
    display_name: str = Field(min_length=1, max_length=MAX_NAME_INPUT_LENGTH)
    roles: list[str] = Field(default_factory=list, max_length=MAX_ROLES)


class CreatedUserResponse(BaseModel):
    user: UserDetailsResponse
    activation: ActivationLinkResponse


class UpdateUserRequest(_Request):
    display_name: str | None = Field(default=None, min_length=1, max_length=MAX_NAME_INPUT_LENGTH)
    status: Literal["active", "disabled"] | None = None


class SetRolesRequest(_Request):
    roles: list[str] = Field(max_length=MAX_ROLES)


class RevokedSessionsResponse(BaseModel):
    revoked: int
