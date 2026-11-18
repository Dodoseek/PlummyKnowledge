"""Helpers shared by the setup and auth routers."""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import Response

from plummy.api.cookies import set_session_cookie
from plummy.application.auth.sessions import IssuedSession
from plummy.core.config import Settings


def no_store(response: Response) -> None:
    """Router dependency: responses carrying session state must never be cached."""
    response.headers["Cache-Control"] = "no-store"


def attach_session_cookie(response: Response, settings: Settings, issued: IssuedSession) -> None:
    set_session_cookie(
        response,
        settings,
        token=issued.token,
        expires_at=issued.context.session_expires_at,
        now=datetime.now(UTC),
    )
