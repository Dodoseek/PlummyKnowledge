"""Google OAuth 2.0 for the Drive connector (ADR-020 §1, §3, §7): Authorization Code with PKCE S256, refresh,
revoke, and the account label. Plain HTTP through ``SafeHttpClient``; no Google SDK.

Tokens, codes, verifiers and the client secret go only into request bodies (never into URLs, logs or errors).
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Sequence
from typing import Final
from urllib.parse import urlencode

import httpx

from plummy.application.sources.ports import (
    ConnectorAuthRevokedError,
    ConnectorEgressDeniedError,
    ConnectorError,
    ConnectorInvalidResponseError,
    ConnectorTransientError,
    OAuthExchangeError,
    OAuthGrant,
)
from plummy.core.network.errors import EgressDeniedError, ResponseTooLargeError
from plummy.core.network.policy import EgressPurpose
from plummy.domain.knowledge import DataSourceType
from plummy.domain.secrets import SecretValue
from plummy.infrastructure.http.safe_client import SafeHttpClient
from plummy.infrastructure.integrations.sources.google_common import (
    DRIVE_READONLY_SCOPE,
    MAX_JSON_BYTES,
    PROVIDER_TYPE,
    TIMEOUT,
    GoogleEndpoints,
    api_error,
    json_object,
)

logger = logging.getLogger(__name__)

FORM: Final = {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}
MAX_TOKEN_LENGTH: Final = 4096
_REVOKED_ERRORS: Final = frozenset({"invalid_grant", "unauthorized_client"})


class GoogleOAuthClient:
    def __init__(
        self,
        http: SafeHttpClient,
        *,
        client_id: str,
        client_secret: SecretValue,
        endpoints: GoogleEndpoints | None = None,
    ) -> None:
        self._http = http
        self._client_id = client_id
        self._client_secret = client_secret
        self._endpoints = endpoints or GoogleEndpoints()

    @property
    def source_type(self) -> DataSourceType:
        return DataSourceType.GOOGLE_DRIVE

    @property
    def required_scopes(self) -> tuple[str, ...]:
        return (DRIVE_READONLY_SCOPE,)

    def has_required_scopes(self, granted: Sequence[str]) -> bool:
        return DRIVE_READONLY_SCOPE in granted

    def authorization_url(self, *, state: str, code_challenge: str, redirect_uri: str) -> str:
        query = urlencode(
            {
                "client_id": self._client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": DRIVE_READONLY_SCOPE,
                "access_type": "offline",  # a refresh token
                "prompt": "consent",  # a new refresh token on every connect
                "include_granted_scopes": "false",  # nothing granted to this client earlier sneaks in
                "state": state,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{self._endpoints.auth_url}?{query}"

    async def _post_form(self, url: str, form: dict[str, str]) -> httpx.Response:
        try:
            return await self._http.request(
                "POST",
                url,
                purpose=EgressPurpose.CONNECTOR,
                provider_type=PROVIDER_TYPE,
                headers=FORM,
                content=urlencode(form).encode("ascii"),
                timeout=TIMEOUT,
                max_response_bytes=MAX_JSON_BYTES,
            )
        except EgressDeniedError:
            raise ConnectorEgressDeniedError() from None
        except ResponseTooLargeError:
            raise ConnectorInvalidResponseError("response too large") from None
        except httpx.HTTPError as exc:
            raise ConnectorTransientError(type(exc).__name__) from None

    def _grant(self, response: httpx.Response, *, refreshing: bool) -> OAuthGrant:
        if response.status_code in (400, 401):
            error = ""
            with contextlib.suppress(ConnectorError):
                error = str(json_object(response).get("error", ""))
            if error in _REVOKED_ERRORS:
                raise ConnectorAuthRevokedError() if refreshing else OAuthExchangeError("invalid_grant")
            raise OAuthExchangeError(f"status {response.status_code}")
        if response.status_code != 200:
            raise ConnectorTransientError(f"status {response.status_code}")
        data = json_object(response)
        access = data.get("access_token")
        if not isinstance(access, str) or not 0 < len(access) <= MAX_TOKEN_LENGTH:
            raise ConnectorInvalidResponseError("no access token")
        refresh = data.get("refresh_token")
        if refresh is not None and (not isinstance(refresh, str) or not 0 < len(refresh) <= MAX_TOKEN_LENGTH):
            raise ConnectorInvalidResponseError("invalid refresh token")
        scope = data.get("scope", "")
        expires = data.get("expires_in", 3600)
        return OAuthGrant(
            access_token=SecretValue(access),
            refresh_token=SecretValue(refresh) if isinstance(refresh, str) else None,
            granted_scopes=tuple(str(scope).split()) if isinstance(scope, str) else (),
            expires_in_seconds=expires if isinstance(expires, int) and expires > 0 else 3600,
        )

    async def exchange_code(
        self, code: SecretValue, *, code_verifier: SecretValue, redirect_uri: str
    ) -> OAuthGrant:
        response = await self._post_form(
            self._endpoints.token_url,
            {
                "grant_type": "authorization_code",
                "code": code.reveal(),
                "code_verifier": code_verifier.reveal(),
                "redirect_uri": redirect_uri,
                "client_id": self._client_id,
                "client_secret": self._client_secret.reveal(),
            },
        )
        return self._grant(response, refreshing=False)

    async def refresh(self, refresh_token: SecretValue) -> OAuthGrant:
        response = await self._post_form(
            self._endpoints.token_url,
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token.reveal(),
                "client_id": self._client_id,
                "client_secret": self._client_secret.reveal(),
            },
        )
        grant = self._grant(response, refreshing=True)
        return OAuthGrant(  # Google keeps the refresh token; the response usually has none
            access_token=grant.access_token,
            refresh_token=grant.refresh_token or refresh_token,
            granted_scopes=grant.granted_scopes,
            expires_in_seconds=grant.expires_in_seconds,
        )

    async def revoke(self, token: SecretValue) -> bool:
        response = await self._post_form(self._endpoints.revoke_url, {"token": token.reveal()})
        if response.status_code == 200:
            return True
        if response.status_code == 400:
            return True  # already invalid: nothing left to revoke
        raise ConnectorTransientError(f"status {response.status_code}")

    async def account_label(self, access_token: SecretValue) -> str | None:
        try:
            response = await self._http.request(
                "GET",
                f"{self._endpoints.api_url}/drive/v3/about",
                purpose=EgressPurpose.CONNECTOR,
                provider_type=PROVIDER_TYPE,
                headers={"Authorization": f"Bearer {access_token.reveal()}", "Accept": "application/json"},
                params={"fields": "user(displayName,emailAddress)"},
                timeout=TIMEOUT,
                max_response_bytes=MAX_JSON_BYTES,
            )
        except EgressDeniedError:
            raise ConnectorEgressDeniedError() from None
        except ResponseTooLargeError:
            raise ConnectorInvalidResponseError("response too large") from None
        except httpx.HTTPError as exc:
            raise ConnectorTransientError(type(exc).__name__) from None
        if response.status_code != 200:
            raise api_error(response)
        user = json_object(response).get("user")
        if not isinstance(user, dict):
            return None
        name, email = user.get("displayName"), user.get("emailAddress")
        name = name if isinstance(name, str) and name.strip() else None
        email = email if isinstance(email, str) and email.strip() else None
        if name and email:
            return f"{name} <{email}>"
        return email or name
