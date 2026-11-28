"""Session cookie, Origin and CSRF checks, and the authentication dependencies (ADR-013).

The frontend is not a security boundary: every check here runs on the server for every request.
"""

from __future__ import annotations

import ipaddress
from dataclasses import dataclass
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, Request

from plummy.api.cookies import (
    clear_session_cookie,
    read_session_token,
    session_cookie_name,
    set_session_cookie,
)
from plummy.api.dependencies import ResourcesDep
from plummy.api.errors import CsrfFailedError
from plummy.application.errors import AuthenticationRequiredError
from plummy.application.security import ClientInfo, SecurityContext
from plummy.core.config import Settings
from plummy.core.security import constant_time_equals, derive_csrf_token
from plummy.domain.identity import Permission
from plummy.domain.identity.models import MAX_USER_AGENT_LENGTH

CSRF_HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

__all__ = [
    "CSRF_HEADER",
    "Authenticated",
    "AuthenticatedDep",
    "CsrfFailedError",
    "CurrentContext",
    "RequirePermission",
    "SettingsDep",
    "clear_session_cookie",
    "client_info",
    "get_authenticated",
    "read_session_token",
    "require_permission",
    "session_cookie_name",
    "set_session_cookie",
    "verify_csrf_token",
    "verify_origin",
]


def get_app_settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


SettingsDep = Annotated[Settings, Depends(get_app_settings)]


def client_info(request: Request) -> ClientInfo:
    """Client address as seen after trusted proxies (uvicorn ``--forwarded-allow-ips``)."""
    ip_address: str | None = None
    if request.client is not None:
        try:
            ip_address = str(ipaddress.ip_address(request.client.host))
        except ValueError:
            ip_address = None
    user_agent = request.headers.get("user-agent")
    return ClientInfo(
        ip_address=ip_address,
        user_agent=user_agent[:MAX_USER_AGENT_LENGTH] if user_agent else None,
    )


def _request_origin(request: Request) -> str | None:
    origin = request.headers.get("origin")
    if origin is None:
        referer = request.headers.get("referer")
        if referer is None:
            return None
        parts = urlsplit(referer)
        if not parts.scheme or not parts.netloc:
            return None
        origin = f"{parts.scheme}://{parts.netloc}"
    return origin.strip().lower()


async def verify_origin(request: Request, settings: SettingsDep) -> None:
    """Router-level dependency: every state-changing request must come from a trusted origin."""
    if request.method in SAFE_METHODS:
        return
    origin = _request_origin(request)
    if origin is None or origin not in settings.trusted_origins:
        raise CsrfFailedError()


def verify_csrf_token(request: Request, session_token: str) -> None:
    supplied = request.headers.get(CSRF_HEADER)
    if not supplied or not constant_time_equals(supplied, derive_csrf_token(session_token)):
        raise CsrfFailedError()


@dataclass(frozen=True, slots=True)
class Authenticated:
    context: SecurityContext
    csrf_token: str


async def get_authenticated(
    request: Request, resources: ResourcesDep, settings: SettingsDep
) -> Authenticated:
    """Valid session required; for unsafe methods also a matching ``X-CSRF-Token``."""
    token = read_session_token(request, settings)
    if token is None:
        raise AuthenticationRequiredError()
    context = await resources.auth.authenticate.execute(token)
    if request.method not in SAFE_METHODS:
        verify_csrf_token(request, token)
    return Authenticated(context=context, csrf_token=derive_csrf_token(token))


AuthenticatedDep = Annotated[Authenticated, Depends(get_authenticated)]


async def get_security_context(authenticated: AuthenticatedDep) -> SecurityContext:
    return authenticated.context


CurrentContext = Annotated[SecurityContext, Depends(get_security_context)]


class RequirePermission:
    """Route dependency for permissions: an early refusal. Use cases still check them (defense in depth).

    A class rather than a closure so the route inventory (``tests/unit/test_route_inventory.py``) can read
    which permissions a route requires.
    """

    __slots__ = ("permissions",)

    def __init__(self, permissions: tuple[Permission, ...]) -> None:
        self.permissions = permissions

    async def __call__(self, context: CurrentContext) -> SecurityContext:
        context.require(*self.permissions)
        return context


def require_permission(*permissions: Permission) -> RequirePermission:
    return RequirePermission(permissions)
