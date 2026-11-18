"""Read-only Instance Settings (ADR-006): set by environment variables, never changed through the API."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from plummy.application.security import SecurityContext
from plummy.domain.identity import Permission


@dataclass(frozen=True, slots=True)
class SecretsSettingsView:
    """Key ring metadata only (ADR-016 §5): never key material."""

    configured: bool
    primary_key_id: str | None
    keys: int


@dataclass(frozen=True, slots=True)
class InstanceSettingsView:
    """Only non-secret values; secrets and connection strings are never part of this view."""

    app_env: str
    public_url: str | None
    setup_mode: str
    session_idle_ttl: timedelta
    session_absolute_ttl: timedelta
    session_cookie_secure: bool
    api_docs_enabled: bool
    secrets: SecretsSettingsView


class GetInstanceSettings:
    def __init__(self, view: InstanceSettingsView) -> None:
        self._view = view

    async def execute(self, context: SecurityContext) -> InstanceSettingsView:
        context.require(Permission.SYSTEM_SETTINGS_READ)
        return self._view
