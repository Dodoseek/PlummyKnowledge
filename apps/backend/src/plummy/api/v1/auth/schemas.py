"""DTOs for ``/api/v1/auth`` (API_CONTRACTS.md §2.2). Requests reject unknown fields."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from plummy.application.auth.account import SessionView
from plummy.application.auth.sessions import IssuedSession
from plummy.application.security import SecurityContext
from plummy.core.security import derive_csrf_token

MAX_EMAIL_INPUT_LENGTH = 320
MAX_PASSWORD_INPUT_LENGTH = 4096  # the policy allows 128; longer input is rejected cheaply


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=1, max_length=MAX_EMAIL_INPUT_LENGTH)
    password: SecretStr = Field(min_length=1, max_length=MAX_PASSWORD_INPUT_LENGTH)


class SessionUser(BaseModel):
    id: UUID
    email: str
    display_name: str


class SessionTimes(BaseModel):
    expires_at: datetime
    idle_expires_at: datetime


class SessionResponse(BaseModel):
    """``Session`` DTO. ``csrf_token`` must be kept in memory only and sent as ``X-CSRF-Token``."""

    user: SessionUser
    roles: list[str]
    permissions: list[str]
    session: SessionTimes
    csrf_token: str

    @classmethod
    def build(cls, context: SecurityContext, csrf_token: str) -> SessionResponse:
        return cls(
            user=SessionUser(id=context.user_id, email=context.email, display_name=context.display_name),
            roles=sorted(context.roles),
            permissions=sorted(context.permissions),
            session=SessionTimes(
                expires_at=context.session_expires_at, idle_expires_at=context.session_idle_expires_at
            ),
            csrf_token=csrf_token,
        )

    @classmethod
    def from_issued(cls, issued: IssuedSession) -> SessionResponse:
        return cls.build(issued.context, derive_csrf_token(issued.token))


MAX_TOKEN_INPUT_LENGTH = 128


class ChangePasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: SecretStr = Field(min_length=1, max_length=MAX_PASSWORD_INPUT_LENGTH)
    new_password: SecretStr = Field(min_length=1, max_length=MAX_PASSWORD_INPUT_LENGTH)


class ActivateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    token: SecretStr = Field(min_length=1, max_length=MAX_TOKEN_INPUT_LENGTH)
    password: SecretStr = Field(min_length=1, max_length=MAX_PASSWORD_INPUT_LENGTH)


class SessionItem(BaseModel):
    """One of the user's own sessions. Token hashes are never exposed."""

    id: UUID
    created_at: datetime
    last_seen_at: datetime
    expires_at: datetime
    user_agent: str | None
    ip_address: str | None
    current: bool

    @classmethod
    def from_view(cls, view: SessionView) -> SessionItem:
        session = view.session
        return cls(
            id=session.id,
            created_at=session.created_at,
            last_seen_at=session.last_seen_at,
            expires_at=session.expires_at,
            user_agent=session.user_agent,
            ip_address=session.ip_address,
            current=view.current,
        )


class SessionListResponse(BaseModel):
    items: list[SessionItem]
