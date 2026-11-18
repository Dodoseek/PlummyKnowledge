"""``/api/v1/setup`` (ADR-006): setup state and one-time initialization with the bootstrap token."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response, status

from plummy.api.dependencies import ResourcesDep
from plummy.api.security import SettingsDep, client_info, read_session_token
from plummy.api.v1.auth.schemas import SessionResponse
from plummy.api.v1.common import attach_session_cookie, no_store
from plummy.api.v1.setup.schemas import InitializeRequest, SetupStatusResponse
from plummy.application.setup.use_cases import InitializeCommand
from plummy.application.setup.validation import AdministratorInput

router = APIRouter(prefix="/setup", tags=["setup"], dependencies=[Depends(no_store)])


@router.get("/status", response_model=SetupStatusResponse)
async def setup_status(resources: ResourcesDep) -> SetupStatusResponse:
    result = await resources.auth.setup_status.execute()
    return SetupStatusResponse(
        status="initialized" if result.initialized else "uninitialized",
        web_setup_enabled=result.web_setup_enabled,
    )


@router.post("/initialize", status_code=status.HTTP_201_CREATED, response_model=SessionResponse)
async def initialize(
    body: InitializeRequest,
    request: Request,
    response: Response,
    resources: ResourcesDep,
    settings: SettingsDep,
) -> SessionResponse:
    issued = await resources.auth.initialize.execute(
        InitializeCommand(
            bootstrap_token=body.bootstrap_token.get_secret_value(),
            admin=AdministratorInput(
                email=body.admin.email,
                display_name=body.admin.display_name,
                password=body.admin.password.get_secret_value(),
            ),
            client=client_info(request),
            previous_token=read_session_token(request, settings),
        )
    )
    attach_session_cookie(response, settings, issued)
    return SessionResponse.from_issued(issued)
