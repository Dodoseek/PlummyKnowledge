"""Ports of external sources (ARCHITECTURE.md §8.6, ADR-020): the OAuth client of a provider, the store of
pending authorizations and the typed connector errors. Adapters live in ``infrastructure``; nothing here knows
Google or HTTP.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar, Protocol
from uuid import UUID

from plummy.domain.knowledge import DataSourceType
from plummy.domain.secrets import SecretValue


class ConnectorError(Exception):
    """A typed failure of a connector call (ADR-020 §9). The message is a safe summary: never a provider
    response body, a file name or a token."""

    code: ClassVar[str] = "connector_error"
    retryable: ClassVar[bool] = False

    def __init__(self, message: str = "") -> None:
        super().__init__(message or self.code)


class ConnectorAuthRevokedError(ConnectorError):
    """The provider refused the refresh token (revoked, expired or the consent was withdrawn)."""

    code = "connector_auth_revoked"


class ConnectorRateLimitedError(ConnectorError):
    code = "connector_rate_limited"
    retryable = True

    def __init__(self, retry_after_seconds: float | None = None) -> None:
        super().__init__("rate limited")
        self.retry_after_seconds = retry_after_seconds


class ConnectorQuotaExceededError(ConnectorError):
    code = "connector_quota_exceeded"


class ConnectorNotFoundError(ConnectorError):
    code = "connector_not_found"


class ConnectorPermissionDeniedError(ConnectorError):
    code = "connector_permission_denied"


class ConnectorTransientError(ConnectorError):
    """Network failure, timeout or a 5xx answer: worth retrying later."""

    code = "connector_unavailable"
    retryable = True


class ConnectorCursorInvalidError(ConnectorError):
    """The saved change cursor is no longer accepted: a reconciliation is needed."""

    code = "connector_cursor_invalid"


class ConnectorEgressDeniedError(ConnectorError):
    """The network policy refused the connection (ADR-007): a configuration problem, not retried."""

    code = "egress_denied"


class ConnectorInvalidResponseError(ConnectorError):
    """The provider answered with something that is not the expected shape (or too large)."""

    code = "connector_invalid_response"


class OAuthExchangeError(ConnectorError):
    """The authorization code (or refresh) was refused for a reason other than a revoked grant."""

    code = "oauth_exchange_failed"


@dataclass(frozen=True, slots=True)
class OAuthGrant:
    """Tokens from the token endpoint. Only ``refresh_token`` is ever stored, encrypted (ADR-020 §7)."""

    access_token: SecretValue
    refresh_token: SecretValue | None
    granted_scopes: tuple[str, ...]
    expires_in_seconds: int


class OAuthClient(Protocol):
    """Authorization Code + PKCE (S256) against one provider."""

    @property
    def source_type(self) -> DataSourceType: ...

    @property
    def required_scopes(self) -> tuple[str, ...]: ...

    def authorization_url(self, *, state: str, code_challenge: str, redirect_uri: str) -> str: ...

    async def exchange_code(
        self, code: SecretValue, *, code_verifier: SecretValue, redirect_uri: str
    ) -> OAuthGrant: ...

    async def refresh(self, refresh_token: SecretValue) -> OAuthGrant: ...

    async def revoke(self, token: SecretValue) -> None: ...

    async def account_label(self, access_token: SecretValue) -> str | None:
        """Who granted the access, for the UI (personal data: never written to logs or audit)."""
        ...


OAuthClients = Mapping[DataSourceType, OAuthClient]  # configured providers only


@dataclass(frozen=True, slots=True)
class PendingAuthorization:
    """Server-side state of one OAuth start, keyed by the hash of ``state`` (ADR-020 §1)."""

    data_source_id: UUID
    user_id: UUID
    session_id: UUID
    code_verifier: str


class OAuthStateUnavailableError(Exception):
    """The state store (Redis) is unavailable."""


class OAuthStateStore(Protocol):
    async def put(self, key: str, pending: PendingAuthorization, *, ttl_seconds: int) -> None: ...

    async def take(self, key: str) -> PendingAuthorization | None:
        """Returns and deletes the entry in one step: a state is usable once."""
        ...
