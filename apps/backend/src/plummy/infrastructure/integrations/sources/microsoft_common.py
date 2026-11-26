"""Microsoft identity platform and Graph endpoints, and the mapping of Graph HTTP answers to typed connector
errors (ADR-021 §2, §4, §9).

Graph's own error text is read only to pick the error type (``error`` of OAuth, ``error.code`` of Graph) and
is then discarded: it may contain file names or addresses. Nothing from a response body is logged.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Final
from urllib.parse import quote

import httpx

from plummy.application.sources.ports import (
    ConnectorAccessTokenRejectedError,
    ConnectorError,
    ConnectorInvalidResponseError,
    ConnectorNotFoundError,
    ConnectorPermissionDeniedError,
    ConnectorQuotaExceededError,
    ConnectorRateLimitedError,
    ConnectorTransientError,
)
from plummy.core.instance_config import Microsoft365Endpoints
from plummy.core.network.policy import RedirectPolicy
from plummy.infrastructure.integrations.sources.connector_http import retry_after

PROVIDER_TYPE: Final = "microsoft_365"
GRAPH_PREFIX: Final = "https://graph.microsoft.com/"
REQUESTED_SCOPES: Final = (
    "offline_access",
    f"{GRAPH_PREFIX}Files.Read.All",
    f"{GRAPH_PREFIX}Sites.Read.All",
    f"{GRAPH_PREFIX}User.Read",
)
# Without these the connector cannot read the chosen folder; User.Read only labels the account.
REQUIRED_SCOPES: Final = ("files.read.all", "sites.read.all")
_QUOTA_CODES: Final = frozenset({"quotaLimitReached"})


def normalize_scope(scope: str) -> str:
    """``https://graph.microsoft.com/Files.Read.All`` and ``files.read.all`` are the same permission."""
    scope = scope.strip()
    if scope.lower().startswith(GRAPH_PREFIX):
        scope = scope[len(GRAPH_PREFIX) :]
    return scope.lower()


@dataclass(frozen=True, slots=True)
class MicrosoftEndpoints:
    auth_url: str
    token_url: str
    api_url: str = "https://graph.microsoft.com/v1.0"
    download_hosts: frozenset[str] = frozenset({"graph.microsoft.com", "*.sharepoint.com"})

    @classmethod
    def for_tenant(cls, tenant: str, override: Microsoft365Endpoints | None = None) -> MicrosoftEndpoints:
        """The single-tenant authority of ``tenant`` (ADR-021 §2), or the test override of CONFIG_FILE."""
        if override is not None:
            return cls(
                auth_url=override.auth_url,
                token_url=override.token_url,
                api_url=override.api_url.rstrip("/"),
                download_hosts=override.download_hosts,
            )
        authority = f"https://login.microsoftonline.com/{quote(tenant, safe='')}/oauth2/v2.0"
        return cls(auth_url=f"{authority}/authorize", token_url=f"{authority}/token")

    @property
    def redirects(self) -> RedirectPolicy:
        return RedirectPolicy(hosts=self.download_hosts)


def _code(response: httpx.Response) -> str:
    try:
        error = json.loads(response.content).get("error", {})
        return str(error.get("code", "")) if isinstance(error, dict) else ""
    except (ValueError, AttributeError, UnicodeDecodeError, TypeError):
        return ""


def api_error(response: httpx.Response) -> ConnectorError:
    """A Graph answer that is not a success, as a typed error."""
    status = response.status_code
    if status == 401:
        return ConnectorAccessTokenRejectedError()
    if status in (429, 503):
        return ConnectorRateLimitedError(retry_after(response))
    if status in (403, 507):
        if _code(response) in _QUOTA_CODES or status == 507:
            return ConnectorQuotaExceededError()
        return ConnectorPermissionDeniedError()
    if status == 404:
        return ConnectorNotFoundError()
    if status >= 500 or status == 408:
        return ConnectorTransientError(f"status {status}")
    return ConnectorInvalidResponseError(f"status {status}")
