"""``/api/v1/system`` (API_CONTRACTS.md §2.17): Instance Settings, read-only and without secrets (ADR-006)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from plummy.api.dependencies import ResourcesDep
from plummy.api.security import require_permission
from plummy.application.security import SecurityContext
from plummy.domain.identity import Permission

router = APIRouter(prefix="/system", tags=["system"])

Reader = Annotated[SecurityContext, Depends(require_permission(Permission.SYSTEM_SETTINGS_READ))]


class SessionSettings(BaseModel):
    idle_ttl_seconds: int
    absolute_ttl_seconds: int
    cookie_secure: bool


class SecretsSettings(BaseModel):
    configured: bool
    primary_key_id: str | None
    keys: int


class InstanceSettingsResponse(BaseModel):
    app_env: str
    public_url: str | None
    setup_mode: str
    session: SessionSettings
    api_docs_enabled: bool
    secrets: SecretsSettings


@router.get("/settings", response_model=InstanceSettingsResponse)
async def get_instance_settings(context: Reader, resources: ResourcesDep) -> InstanceSettingsResponse:
    view = await resources.admin.instance_settings.execute(context)
    return InstanceSettingsResponse(
        app_env=view.app_env,
        public_url=view.public_url,
        setup_mode=view.setup_mode,
        session=SessionSettings(
            idle_ttl_seconds=int(view.session_idle_ttl.total_seconds()),
            absolute_ttl_seconds=int(view.session_absolute_ttl.total_seconds()),
            cookie_secure=view.session_cookie_secure,
        ),
        api_docs_enabled=view.api_docs_enabled,
        secrets=SecretsSettings(
            configured=view.secrets.configured,
            primary_key_id=view.secrets.primary_key_id,
            keys=view.secrets.keys,
        ),
    )
