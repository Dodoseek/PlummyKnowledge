"""``/api/v1/auth``: login, logout, own sessions, password change, activation (ADR-013, ADR-015).

Thin adapters over use cases.
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status

from plummy.api.dependencies import ResourcesDep
from plummy.api.security import (
    AuthenticatedDep,
    CurrentContext,
    SettingsDep,
    clear_session_cookie,
    client_info,
    limit_by_address,
    read_session_token,
    verify_csrf_token,
)
from plummy.api.v1.auth.schemas import (
    ActivateRequest,
    ChangePasswordRequest,
    LoginRequest,
    SessionItem,
    SessionListResponse,
    SessionResponse,
)
from plummy.api.v1.common import attach_session_cookie, no_store
from plummy.application.auth.account import ChangePasswordCommand
from plummy.application.auth.activation import ActivateCommand
from plummy.application.auth.use_cases import LoginCommand
from plummy.application.errors import AuthenticationRequiredError

router = APIRouter(prefix="/auth", tags=["auth"], dependencies=[Depends(no_store)])


@router.post("/login", response_model=SessionResponse, dependencies=[Depends(limit_by_address)])
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


@router.post(
    "/logout",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
    dependencies=[Depends(limit_by_address)],
)
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


@router.get("/sessions", response_model=SessionListResponse)
async def list_my_sessions(context: CurrentContext, resources: ResourcesDep) -> SessionListResponse:
    views = await resources.auth.list_sessions.execute(context)
    return SessionListResponse(items=[SessionItem.from_view(view) for view in views])


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def revoke_my_session(
    session_id: UUID,
    request: Request,
    response: Response,
    context: CurrentContext,
    resources: ResourcesDep,
    settings: SettingsDep,
) -> None:
    """Another user's session is reported as ``404``, exactly like a missing one."""
    await resources.auth.revoke_session.execute(context, client_info(request), session_id)
    if session_id == context.session_id:
        clear_session_cookie(response, settings)


@router.post("/password/change", response_model=SessionResponse)
async def change_password(
    body: ChangePasswordRequest,
    request: Request,
    response: Response,
    context: CurrentContext,
    resources: ResourcesDep,
    settings: SettingsDep,
) -> SessionResponse:
    """All sessions are revoked; this browser gets a new session cookie and CSRF token."""
    issued = await resources.auth.change_password.execute(
        context,
        ChangePasswordCommand(
            current_password=body.current_password.get_secret_value(),
            new_password=body.new_password.get_secret_value(),
            client=client_info(request),
        ),
    )
    attach_session_cookie(response, settings, issued)
    return SessionResponse.from_issued(issued)


@router.post("/activate", response_model=SessionResponse, dependencies=[Depends(limit_by_address)])
async def activate(
    body: ActivateRequest,
    request: Request,
    response: Response,
    resources: ResourcesDep,
    settings: SettingsDep,
) -> SessionResponse:
    """Public: the one-time token from the activation link proves the right to set the first password."""
    issued = await resources.auth.activate.execute(
        ActivateCommand(
            token=body.token.get_secret_value(),
            password=body.password.get_secret_value(),
            client=client_info(request),
            previous_token=read_session_token(request, settings),
        )
    )
    attach_session_cookie(response, settings, issued)
    return SessionResponse.from_issued(issued)
