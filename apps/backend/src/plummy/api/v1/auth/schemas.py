"""DTOs for ``/api/v1/auth`` (API_CONTRACTS.md §2.2). Requests reject unknown fields."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr

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
