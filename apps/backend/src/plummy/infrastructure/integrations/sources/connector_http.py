"""The HTTP transport shared by the source connectors (ADR-020 §4, §9; ADR-021 §4): JSON requests with
retries and the bounded download. Plain HTTP through ``SafeHttpClient`` with the ``connector`` purpose.

- ``429``, ``5xx``, the provider's rate-limit answers and network failures are retried with exponential
  backoff and jitter (``Retry-After`` first), up to ``max_attempts`` per request; the rest fails at once. A
  rejected access token is never retried here: the caller refreshes it.
- Downloads follow redirects only to the connector's download hosts, ``https`` only, ``Authorization`` is
  dropped on another host (``SafeHttpClient``); the size limit applies to the whole stream.
- Response bodies are parsed strictly and never logged: they contain file names and account data.
"""

from __future__ import annotations

import asyncio
import email.utils
import json
import math
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from typing import Any, Final

import httpx

from plummy.application.sources.ports import (
    ConnectorAccessTokenRejectedError,
    ConnectorEgressDeniedError,
    ConnectorError,
    ConnectorFileTooLargeError,
    ConnectorInvalidResponseError,
    ConnectorRateLimitedError,
    ConnectorTransientError,
)
from plummy.core.network.errors import EgressDeniedError, ResponseTooLargeError
from plummy.core.network.policy import EgressPurpose, RedirectPolicy
from plummy.domain.secrets import SecretValue
from plummy.infrastructure.http.safe_client import SafeHttpClient

MAX_JSON_BYTES: Final = 4 * 1024 * 1024
TIMEOUT: Final = httpx.Timeout(30.0, connect=10.0)
DOWNLOAD_TIMEOUT: Final = httpx.Timeout(120.0, connect=10.0)
MAX_BACKOFF_SECONDS: Final = 60.0

Sleep = Callable[[float], Awaitable[None]]
ErrorMapper = Callable[[httpx.Response], ConnectorError]


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


class ConnectorTransport:
    def __init__(
        self,
        http: SafeHttpClient,
        *,
        provider_type: str,
        api_error: ErrorMapper,
        max_attempts: int = 5,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self._http = http
        self._provider_type = provider_type
        self._api_error = api_error
        self._max_attempts = max_attempts
        self._sleep = sleep

    @staticmethod
    def _delay(attempt: int, error: ConnectorError) -> float:
        if isinstance(error, ConnectorRateLimitedError) and error.retry_after_seconds is not None:
            return min(MAX_BACKOFF_SECONDS, error.retry_after_seconds)
        base = min(MAX_BACKOFF_SECONDS / 2, 2.0 ** (attempt - 1))
        return base * random.uniform(0.5, 1.5)  # noqa: S311 - jitter, not security

    def _retry(self, attempt: int, error: ConnectorError) -> bool:
        return (
            error.retryable
            and not isinstance(error, ConnectorAccessTokenRejectedError)
            and attempt < self._max_attempts
        )

    async def _get_once(
        self, access_token: SecretValue, url: str, params: Mapping[str, str], on_error: ErrorMapper | None
    ) -> dict[str, Any]:
        try:
            response = await self._http.request(
                "GET",
                url,
                purpose=EgressPurpose.CONNECTOR,
                provider_type=self._provider_type,
                headers={"Authorization": f"Bearer {access_token.reveal()}", "Accept": "application/json"},
                params=params,
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
            raise (on_error(response) if on_error else self._api_error(response))
        return json_object(response)

    async def get_json(
        self,
        access_token: SecretValue,
        url: str,
        params: Mapping[str, str],
        on_error: ErrorMapper | None = None,
    ) -> dict[str, Any]:
        """A JSON object from ``GET url``; ``on_error`` maps a non-200 answer instead of ``api_error``."""
        attempt = 0
        while True:
            attempt += 1
            try:
                return await self._get_once(access_token, url, params, on_error)
            except ConnectorError as exc:
                if not self._retry(attempt, exc):
                    raise
                await self._sleep(self._delay(attempt, exc))

    async def download(
        self,
        access_token: SecretValue,
        url: str,
        params: Mapping[str, str],
        *,
        max_bytes: int,
        redirects: RedirectPolicy,
    ) -> AsyncIterator[bytes]:
        """The body of ``GET url``, at most ``max_bytes``; retried only before the first byte."""
        attempt, started = 0, False
        while True:
            attempt += 1
            try:
                async with self._http.stream(
                    "GET",
                    url,
                    purpose=EgressPurpose.CONNECTOR,
                    provider_type=self._provider_type,
                    headers={"Authorization": f"Bearer {access_token.reveal()}"},
                    params=params,
                    timeout=DOWNLOAD_TIMEOUT,
                    max_response_bytes=max_bytes,
                    redirects=redirects,
                ) as response:
                    if response.status_code != 200:
                        await response.aread()
                        raise self._api_error(response)
                    async for chunk in response.aiter_bytes():
                        started = True
                        yield chunk
                    return
            except EgressDeniedError:
                raise ConnectorEgressDeniedError() from None
            except ResponseTooLargeError:
                raise ConnectorFileTooLargeError() from None
            except httpx.HTTPError as exc:
                error: ConnectorError = ConnectorTransientError(type(exc).__name__)
                if started or not self._retry(attempt, error):
                    raise error from None
            except ConnectorError as exc:
                if started or not self._retry(attempt, exc):
                    raise
                error = exc
            await self._sleep(self._delay(attempt, error))
