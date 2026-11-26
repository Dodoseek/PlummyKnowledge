"""A working connection to a source: an access token from the stored refresh token (ADR-020 §7).

The access token is returned to the caller and lives only in its memory; nothing but the refresh token (in the
vault) and the refresh time is written. A refused refresh token marks the credentials ``revoked`` and the
source ``error`` (``connector_auth_revoked``) until it is connected again.
"""

from __future__ import annotations

import logging
import re
from typing import Final
from uuid import UUID

from plummy.application.errors import ApplicationError, ConflictError, UpstreamProviderError
from plummy.application.ports import Clock, UnitOfWorkFactory
from plummy.application.secret_storage.vault import SecretVault
from plummy.application.sources.ports import (
    ConnectorAuthRevokedError,
    ConnectorError,
    Connectors,
    DataSourceConnector,
    OAuthClient,
    OAuthClients,
)
from plummy.domain.knowledge import DataSource, DataSourceStatus
from plummy.domain.secrets import SecretValue
from plummy.domain.sources import CredentialStatus

logger = logging.getLogger(__name__)

AUTH_REVOKED: Final = "connector_auth_revoked"
# Google IDs, and the opaque ``site:``/``drive:``/``item:`` identifiers of Microsoft Graph (ADR-021 §6).
_REMOTE_ID: Final = re.compile(r"[A-Za-z0-9_!:,.-]{1,512}")
_PAGE_TOKEN: Final = re.compile(r"[!-~]{1,2048}")  # printable ASCII, no spaces


class SourceNotConnectedError(ConflictError):
    """The source has no valid credentials: connect it (OAuth) first."""

    code = "source_not_connected"


class SourceAuthRevokedError(ConflictError):
    """The provider no longer accepts the stored grant: connect the source again."""

    code = AUTH_REVOKED


class ConnectorNotConfiguredError(ConflictError):
    """The OAuth client of this source type is not configured on the server (Instance Settings)."""

    code = "connector_not_configured"


class ConnectorCallFailedError(UpstreamProviderError):
    """A provider call needed by the request failed (``502``); ``code`` is the typed connector code."""

    code = "connector_error"

    def __init__(self, error: ConnectorError) -> None:
        super().__init__(error.code, "The data source provider request failed.")
        self.code = error.code


def plausible_remote_id(value: str) -> bool:
    return _REMOTE_ID.fullmatch(value) is not None


def plausible_page_token(value: str) -> bool:
    return _PAGE_TOKEN.fullmatch(value) is not None


def connector_failure(error: ConnectorError) -> ApplicationError:
    if isinstance(error, ConnectorAuthRevokedError):
        return SourceAuthRevokedError()
    return ConnectorCallFailedError(error)


def provider_of(
    clients: OAuthClients, connectors: Connectors, source: DataSource
) -> tuple[OAuthClient, DataSourceConnector]:
    oauth, connector = clients.get(source.type), connectors.get(source.type)
    if oauth is None or connector is None:
        raise ConnectorNotConfiguredError()
    return oauth, connector


class AccessTokens:
    """Refreshes an access token for one source. Provider calls run outside database transactions."""

    def __init__(self, uow: UnitOfWorkFactory, clock: Clock, vault: SecretVault) -> None:
        self._uow, self._clock, self._vault = uow, clock, vault

    async def get(self, oauth: OAuthClient, source_id: UUID) -> SecretValue:
        """Raises :class:`SourceNotConnectedError` without valid credentials and
        :class:`ConnectorAuthRevokedError` (after recording it) when the grant is refused."""
        async with self._uow() as uow:
            credentials = await uow.source_credentials.get(source_id)
            if credentials is None or credentials.status is not CredentialStatus.VALID:
                raise SourceNotConnectedError()
            refresh_token = await self._vault.reveal(uow, credentials.secret_ref_id)
        try:
            grant = await oauth.refresh(refresh_token)
        except ConnectorAuthRevokedError:
            await self._revoked(source_id)
            raise
        now = self._clock.now()
        async with self._uow() as uow:
            current = await uow.source_credentials.get(source_id)
            if current is None or current.secret_ref_id != credentials.secret_ref_id:
                return grant.access_token  # disconnected or reconnected meanwhile: nothing to update
            current.last_refreshed_at = now
            current.updated_at = now
            if grant.refresh_token is not None and grant.refresh_token.reveal() != refresh_token.reveal():
                await self._vault.replace(uow, current.secret_ref_id, grant.refresh_token)  # rotated
            await uow.source_credentials.save(current)
            await uow.commit()
        return grant.access_token

    async def _revoked(self, source_id: UUID) -> None:
        now = self._clock.now()
        async with self._uow() as uow:
            credentials = await uow.source_credentials.get(source_id)
            source = await uow.data_sources.get_for_update(source_id)
            if credentials is not None and credentials.status is not CredentialStatus.REVOKED:
                credentials.status = CredentialStatus.REVOKED
                credentials.revoked_at = now
                credentials.updated_at = now
                await uow.source_credentials.save(credentials)
            if source is not None and source.deleted_at is None:
                source.status = DataSourceStatus.ERROR
                source.last_error = AUTH_REVOKED
                source.next_sync_at = None
                await uow.data_sources.save(source, at=now)
            await uow.commit()
        logger.warning("refresh token refused by the provider", extra={"data_source_id": str(source_id)})
