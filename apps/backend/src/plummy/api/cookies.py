"""Session cookie (ADR-013 §1): ``__Host-pk_session``, or ``pk_session`` for plain-HTTP development."""

from __future__ import annotations

from datetime import datetime

from fastapi import Request, Response

from plummy.core.config import Settings
from plummy.core.security import is_well_formed_token

SECURE_COOKIE_NAME = "__Host-pk_session"
INSECURE_COOKIE_NAME = "pk_session"


def session_cookie_name(settings: Settings) -> str:
    # ``__Host-`` requires Secure; plain-HTTP development uses an unprefixed name (never in production).
    return SECURE_COOKIE_NAME if settings.session_cookie_secure else INSECURE_COOKIE_NAME


def read_session_token(request: Request, settings: Settings) -> str | None:
    token = request.cookies.get(session_cookie_name(settings))
    return token if token is not None and is_well_formed_token(token) else None


def set_session_cookie(
    response: Response, settings: Settings, *, token: str, expires_at: datetime, now: datetime
) -> None:
    response.set_cookie(
        session_cookie_name(settings),
        token,
        max_age=max(0, int((expires_at - now).total_seconds())),
        path="/",
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(
        session_cookie_name(settings),
        path="/",
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )
