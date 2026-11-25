"""Google endpoints and the mapping of Google HTTP answers to typed connector errors (ADR-020 §4, §9).

Google's own error text is read only to pick the error type (``error`` of OAuth, ``reason`` of the Drive API)
and is then discarded: it may contain file names or addresses. Nothing from a response body is logged.
"""

from __future__ import annotations

import email.utils
import json
import math
import time
from dataclasses import dataclass
from typing import Any, Final

import httpx

from plummy.application.sources.ports import (
    ConnectorError,
    ConnectorInvalidResponseError,
    ConnectorNotFoundError,
    ConnectorPermissionDeniedError,
    ConnectorQuotaExceededError,
    ConnectorRateLimitedError,
    ConnectorTransientError,
)
from plummy.core.instance_config import GoogleDriveEndpoints
from plummy.core.network.policy import RedirectPolicy

PROVIDER_TYPE: Final = "google_drive"
DRIVE_READONLY_SCOPE: Final = "https://www.googleapis.com/auth/drive.readonly"
MAX_JSON_BYTES: Final = 4 * 1024 * 1024
TIMEOUT: Final = httpx.Timeout(30.0, connect=10.0)
_RATE_REASONS: Final = frozenset({"rateLimitExceeded", "userRateLimitExceeded"})
_QUOTA_REASONS: Final = frozenset({"dailyLimitExceeded", "quotaExceeded", "storageQuotaExceeded"})


@dataclass(frozen=True, slots=True)
class GoogleEndpoints:
    auth_url: str = "https://accounts.google.com/o/oauth2/v2/auth"
    token_url: str = "https://oauth2.googleapis.com/token"  # noqa: S105 - an endpoint, not a secret
    revoke_url: str = "https://oauth2.googleapis.com/revoke"
    api_url: str = "https://www.googleapis.com"
    download_hosts: frozenset[str] = frozenset({"www.googleapis.com", "*.googleusercontent.com"})

    @classmethod
    def from_config(cls, override: GoogleDriveEndpoints | None) -> GoogleEndpoints:
        if override is None:
            return cls()
        return cls(
            auth_url=override.auth_url,
            token_url=override.token_url,
            revoke_url=override.revoke_url,
            api_url=override.api_url.rstrip("/"),
            download_hosts=override.download_hosts,
        )

    @property
    def redirects(self) -> RedirectPolicy:
        return RedirectPolicy(hosts=self.download_hosts)


def json_object(response: httpx.Response) -> dict[str, Any]:
    try:
        data = json.loads(response.content)
    except (ValueError, UnicodeDecodeError):
        raise ConnectorInvalidResponseError("not JSON") from None
    if not isinstance(data, dict):
        raise ConnectorInvalidResponseError("not a JSON object")
    return data


def retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if value is None:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        moment = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    seconds = moment.timestamp() - time.time()
    return max(0.0, seconds) if math.isfinite(seconds) else None


def _reasons(response: httpx.Response) -> set[str]:
    try:
        error = json.loads(response.content).get("error", {})
        return {str(item.get("reason")) for item in error.get("errors", []) if isinstance(item, dict)}
    except (ValueError, AttributeError, UnicodeDecodeError, TypeError):
        return set()


def api_error(response: httpx.Response) -> ConnectorError:
    """A Drive API answer that is not a success, as a typed error."""
    status = response.status_code
    if status == 429:
        return ConnectorRateLimitedError(retry_after(response))
    if status == 403:
        reasons = _reasons(response)
        if reasons & _RATE_REASONS:
            return ConnectorRateLimitedError(retry_after(response))
        if reasons & _QUOTA_REASONS:
            return ConnectorQuotaExceededError()
        return ConnectorPermissionDeniedError()
    if status == 404:
        return ConnectorNotFoundError()
    if status >= 500 or status == 408:
        return ConnectorTransientError(f"status {status}")
    return ConnectorInvalidResponseError(f"status {status}")
