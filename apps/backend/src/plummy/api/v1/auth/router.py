"""``/api/v1/auth``: login, logout, current session (ADR-013). Thin adapters over use cases."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status

from plummy.api.dependencies import ResourcesDep
from plummy.api.security import (
    AuthenticatedDep,
    SettingsDep,
    clear_session_cookie,
    client_info,
    read_session_token,
    verify_csrf_token,
)
from plummy.api.v1.auth.schemas import LoginRequest, SessionResponse
from plummy.api.v1.common import attach_session_cookie, no_store
from plummy.application.auth.use_cases import LoginCommand
from plummy.application.errors import AuthenticationRequiredError

router = APIRouter(prefix="/auth", tags=["auth"], dependencies=[Depends(no_store)])


@router.post("/login", response_model=SessionResponse)
async def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    resources: ResourcesDep,
    settings: SettingsDep,
) -> SessionResponse:
    issued = await resources.auth.login.execute(
        LoginCommand(
            email=body.email,
            password=body.password.get_secret_value(),
            client=client_info(request),
            previous_token=read_session_token(request, settings),
        )
    )
    attach_session_cookie(response, settings, issued)
    return SessionResponse.from_issued(issued)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def logout(
    request: Request, response: Response, resources: ResourcesDep, settings: SettingsDep
) -> None:
    """Idempotent: without a valid session it only clears the cookie."""
    token = read_session_token(request, settings)
    if token is not None:
        try:
            context = await resources.auth.authenticate.execute(token)
        except AuthenticationRequiredError:
            context = None
        if context is not None:
            verify_csrf_token(request, token)
            await resources.auth.logout.execute(context, client_info(request))
    clear_session_cookie(response, settings)


@router.get("/me", response_model=SessionResponse)
async def me(authenticated: AuthenticatedDep) -> SessionResponse:
    return SessionResponse.build(authenticated.context, authenticated.csrf_token)
