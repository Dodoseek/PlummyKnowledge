"""Shared plumbing of the provider adapters: HTTP with retries, error mapping, stream parsing, cancellation.

Error messages never contain response bodies, prompts or keys: a provider's own error text is discarded and
only a typed :class:`ProviderError` with a fixed message leaves the adapter. Logs carry the provider type,
the HTTP status and the attempt number only.
"""

from __future__ import annotations

import asyncio
import email.utils
import html
import inspect
import json
import logging
import math
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Final, Literal

import httpx

from plummy.application.ai.ports import (
    CancellationToken,
    ChatRequest,
    ConnectionTestResult,
    ContextDocument,
    ProviderAuthError,
    ProviderBadRequestError,
    ProviderCancelledError,
    ProviderConnection,
    ProviderEgressDeniedError,
    ProviderError,
    ProviderInvalidResponseError,
    ProviderModelNotFoundError,
    ProviderRateLimitedError,
    ProviderTimeoutError,
    ProviderUnavailableError,
)
from plummy.core.network.errors import EgressDeniedError, ResponseTooLargeError
from plummy.core.network.policy import EgressPurpose
from plummy.domain.ai import ProviderType
from plummy.infrastructure.http.safe_client import SafeHttpClient

logger = logging.getLogger(__name__)

CONNECT_TIMEOUT: Final = 10.0
MAX_JSON_RESPONSE_BYTES: Final = 10 * 1024 * 1024
MAX_STREAM_BYTES: Final = 32 * 1024 * 1024
MAX_EMBEDDING_RESPONSE_BYTES: Final = 64 * 1024 * 1024
MAX_LISTED_MODELS: Final = 500
MAX_MODEL_ID_LENGTH: Final = 256

type Sleep = Callable[[float], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """Retries on 408/429/5xx and transport errors, before any data is consumed; honours ``Retry-After``."""

    attempts: int = 3
    base_delay: float = 0.5
    max_delay: float = 8.0
    max_retry_after: float = 30.0  # a longer Retry-After is not waited for: the error goes to the caller
    sleep: Sleep = asyncio.sleep
    jitter: Callable[[], float] = field(default=random.random)

    def delay(self, attempt: int, error: ProviderError) -> float | None:
        """Seconds to wait before attempt ``attempt + 1``, or ``None`` when the error must be raised."""
        if not error.retryable or attempt >= self.attempts:
            return None
        if error.retry_after is not None:
            return error.retry_after if error.retry_after <= self.max_retry_after else None
        backoff = min(self.max_delay, self.base_delay * 2.0 ** (attempt - 1))
        return backoff * (0.5 + self.jitter() / 2)


NO_RETRY: Final = RetryPolicy(attempts=1)


def parse_retry_after(value: str | None, *, now: float | None = None) -> float | None:
    if not value:
        return None
    value = value.strip()
    try:
        seconds = float(value)
    except ValueError:
        try:
            moment = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        seconds = moment.timestamp() - (time.time() if now is None else now)
    if not math.isfinite(seconds):
        return None
    return max(0.0, seconds)


def error_for_status(status: int, headers: Mapping[str, str], *, model_scoped: bool) -> ProviderError:
    """Maps an unsuccessful HTTP status to a typed error. The response body is deliberately ignored."""
    if status in (401, 403):
        return ProviderAuthError()
    if status == 404:
        return (
            ProviderModelNotFoundError()
            if model_scoped
            else ProviderBadRequestError("The provider endpoint was not found. Check the base URL.")
        )
    if status == 408:
        return ProviderTimeoutError()
    if status == 429:
        return ProviderRateLimitedError(retry_after=parse_retry_after(headers.get("retry-after")))
    if status >= 500:
        return ProviderUnavailableError(retry_after=parse_retry_after(headers.get("retry-after")))
    if 400 <= status < 500:
        return ProviderBadRequestError()
    return ProviderInvalidResponseError()  # 1xx/3xx: redirects are not followed (ADR-007)


def error_for_exception(exc: Exception) -> ProviderError:
    if isinstance(exc, ProviderError):
        return exc
    if isinstance(exc, EgressDeniedError):
        return ProviderEgressDeniedError()
    if isinstance(exc, ResponseTooLargeError):
        return ProviderInvalidResponseError("The provider response exceeded the size limit.")
    if isinstance(exc, httpx.TimeoutException):
        return ProviderTimeoutError()
    if isinstance(exc, (httpx.TransportError, httpx.StreamError)):
        return ProviderUnavailableError()
    if isinstance(exc, (json.JSONDecodeError, UnicodeDecodeError)):
        return ProviderInvalidResponseError()
    raise exc


def http_timeout(read: float) -> httpx.Timeout:
    return httpx.Timeout(read, connect=min(CONNECT_TIMEOUT, read))


async def race[T](cancel: CancellationToken, awaitable: Awaitable[T]) -> T:
    """Awaits ``awaitable`` unless ``cancel`` fires first (then the work is cancelled too)."""
    if cancel.cancelled:
        if inspect.iscoroutine(awaitable):
            awaitable.close()  # never started: no "coroutine was never awaited" warning
        raise ProviderCancelledError
    work = asyncio.ensure_future(awaitable)
    stopper = asyncio.ensure_future(cancel.wait())
    try:
        await asyncio.wait({work, stopper}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stopper.cancel()
        if not work.done():
            work.cancel()
            await asyncio.gather(work, return_exceptions=True)
    if work.cancelled():
        raise ProviderCancelledError
    return work.result()


class ProviderHttp:
    """HTTP calls of one adapter: base URL, auth headers, egress purpose, retries and error mapping."""

    def __init__(
        self,
        client: SafeHttpClient,
        connection: ProviderConnection,
        headers: Mapping[str, str],
        retry: RetryPolicy,
    ) -> None:
        self._client = client
        self._connection = connection
        self._headers = dict(headers)
        self.retry = retry

    @property
    def provider_type(self) -> ProviderType:
        return self._connection.provider_type

    def _log_retry(self, attempt: int, error: ProviderError, delay: float, status: int | None) -> None:
        logger.warning(
            "Provider request failed, retrying",
            extra={
                "provider_type": str(self.provider_type),
                "error_code": str(error.code),
                "status": status,
                "attempt": attempt,
                "delay": round(delay, 3),
            },
        )

    async def json(
        self,
        method: Literal["GET", "POST"],
        path: str,
        *,
        purpose: EgressPurpose,
        timeout: float,  # noqa: ASYNC109 - per-phase read timeout
        body: Any = None,
        model_scoped: bool = False,
        retry: RetryPolicy | None = None,
        max_response_bytes: int = MAX_JSON_RESPONSE_BYTES,
    ) -> Any:
        policy = retry or self.retry
        attempt = 1
        while True:
            status: int | None = None
            try:
                response = await self._client.request(
                    method,
                    self._connection.base_url + path,
                    purpose=purpose,
                    provider_type=str(self.provider_type),
                    headers=self._headers,
                    json=body,
                    timeout=http_timeout(timeout),
                    max_response_bytes=max_response_bytes,
                )
                status = response.status_code
                if not response.is_success:
                    raise error_for_status(status, response.headers, model_scoped=model_scoped)
                return response.json()
            except Exception as exc:
                error = error_for_exception(exc)
                delay = policy.delay(attempt, error)
                if delay is None:
                    raise error from None
                self._log_retry(attempt, error, delay, status)
                await policy.sleep(delay)
                attempt += 1

    @asynccontextmanager
    async def stream(
        self,
        path: str,
        *,
        purpose: EgressPurpose,
        timeout: float,  # noqa: ASYNC109 - per-read timeout of the stream
        body: Any,
        cancel: CancellationToken,
    ) -> AsyncIterator[AsyncIterator[str]]:
        """Opens a streamed POST, retrying until a successful status, and yields its lines.

        Cancellation is checked before every attempt and races every read and retry pause; while a
        connection is being set up it takes effect once the response headers arrive (bounded by ``timeout``).
        """
        attempt = 1
        while True:
            cancel.raise_if_cancelled()
            status: int | None = None
            opened = False
            try:
                async with self._client.stream(
                    "POST",
                    self._connection.base_url + path,
                    purpose=purpose,
                    provider_type=str(self.provider_type),
                    headers=self._headers,
                    json=body,
                    timeout=http_timeout(timeout),
                    max_response_bytes=MAX_STREAM_BYTES,
                ) as response:
                    status = response.status_code
                    if not response.is_success:
                        raise error_for_status(status, response.headers, model_scoped=True)
                    opened = True
                    yield _guarded_lines(response, cancel)
                    return
            except Exception as exc:
                if opened or isinstance(exc, ProviderCancelledError):
                    raise  # once data flows nothing is retried: the caller may have consumed events
                error = error_for_exception(exc)
                delay = self.retry.delay(attempt, error)
                if delay is None:
                    raise error from None
                self._log_retry(attempt, error, delay, status)
                await race(cancel, self.retry.sleep(delay))
                attempt += 1


async def _guarded_lines(response: httpx.Response, cancel: CancellationToken) -> AsyncIterator[str]:
    lines = response.aiter_lines()
    while True:
        try:
            line = await race(cancel, anext(lines))
        except StopAsyncIteration:
            return
        except ProviderError:
            raise
        except Exception as exc:
            raise error_for_exception(exc) from None
        yield line


@dataclass(frozen=True, slots=True)
class SseEvent:
    event: str
    data: str


async def iter_sse(lines: AsyncIterator[str]) -> AsyncIterator[SseEvent]:
    """Server-sent events (WHATWG): ``event:`` / ``data:`` fields, dispatched on a blank line."""
    event: str = "message"
    data: list[str] = []
    async for line in lines:
        if not line:
            if data:
                yield SseEvent(event, "\n".join(data))
            event, data = "message", []
            continue
        if line.startswith(":"):
            continue
        name, _, value = line.partition(":")
        value = value.removeprefix(" ")
        if name == "event":
            event = value
        elif name == "data":
            data.append(value)
    if data:
        yield SseEvent(event, "\n".join(data))


def parse_json_object(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        raise ProviderInvalidResponseError from None
    if not isinstance(value, dict):
        raise ProviderInvalidResponseError
    return value


def optional_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def model_ids(items: Any, key: str) -> tuple[str, ...]:
    """Model ids from a provider listing, bounded in count and length; malformed entries are skipped."""
    if not isinstance(items, list):
        raise ProviderInvalidResponseError
    ids: list[str] = []
    for item in items[:MAX_LISTED_MODELS]:
        value = item.get(key) if isinstance(item, dict) else None
        if isinstance(value, str) and 0 < len(value) <= MAX_MODEL_ID_LENGTH and value.isprintable():
            ids.append(value)
    return tuple(ids)


def vectors(items: Any, expected: int) -> list[list[float]]:
    """Validates embedding vectors: one per input, same dimension, finite numbers."""
    if not isinstance(items, list) or len(items) != expected:
        raise ProviderInvalidResponseError
    result: list[list[float]] = []
    dimension: int | None = None
    for item in items:
        if not isinstance(item, list) or not item:
            raise ProviderInvalidResponseError
        vector: list[float] = []
        for number in item:
            if isinstance(number, bool) or not isinstance(number, (int, float)) or not math.isfinite(number):
                raise ProviderInvalidResponseError
            vector.append(float(number))
        if dimension is None:
            dimension = len(vector)
        elif len(vector) != dimension:
            raise ProviderInvalidResponseError
        result.append(vector)
    return result


async def measure_connection(
    probe: Callable[[], Awaitable[tuple[str, ...]]],
    *,
    timeout: float,  # noqa: ASYNC109 - overall budget of the test
    provider_type: ProviderType,
) -> ConnectionTestResult:
    """Runs a connection probe within ``timeout``; never raises, failures become typed results."""
    started = time.perf_counter()
    try:
        async with asyncio.timeout(timeout):
            models = await probe()
    except TimeoutError:
        error: ProviderError | None = ProviderTimeoutError()
        models = ()
    except ProviderError as exc:
        error, models = exc, ()
    else:
        error = None
    latency_ms = round((time.perf_counter() - started) * 1000)
    if error is not None:
        logger.info(
            "Provider connection test failed",
            extra={"provider_type": str(provider_type), "error_code": str(error.code)},
        )
    return ConnectionTestResult(ok=error is None, latency_ms=latency_ms, models=models, error=error)


def chunked[T](items: Sequence[T], size: int) -> list[Sequence[T]]:
    return [items[start : start + size] for start in range(0, len(items), size)]


# --- prompt layout --------------------------------------------------------------------------------------


def render_context(documents: Sequence[ContextDocument]) -> str:
    """Retrieved fragments as a delimited block. Text is escaped so a document cannot close its own tag."""
    parts = ["<documents>"]
    for document in documents:
        title = f' title="{html.escape(document.title, quote=True)}"' if document.title else ""
        parts.append(
            f'<document id="{html.escape(document.id, quote=True)}"{title}>\n'
            f"{html.escape(document.text, quote=False)}\n</document>"
        )
    parts.append("</documents>")
    return "\n".join(parts)


def check_chat_request(request: ChatRequest) -> None:
    if not request.messages or request.messages[-1].role != "user":
        raise ValueError("a chat request must end with a user message")
    if request.max_output_tokens is not None and request.max_output_tokens < 1:
        raise ValueError("max_output_tokens must be positive")


def plain_messages(request: ChatRequest) -> list[dict[str, str]]:
    """History plus the last user turn, with the context block put in front of the question.

    Used by OpenAI-style and Ollama APIs; instructions travel separately as the system message, so retrieved
    text never shares a message with them.
    """
    check_chat_request(request)
    messages = [{"role": message.role, "content": message.content} for message in request.messages]
    if request.context:
        last = messages[-1]
        last["content"] = render_context(request.context) + "\n\n" + last["content"]
    return messages
