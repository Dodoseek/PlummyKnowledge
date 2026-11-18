"""``LLMProvider`` / ``EmbeddingProvider`` ports: requests, events and typed errors (ARCHITECTURE.md §8).

Cloud and local models use the same interfaces; the only difference is the network policy (ADR-007). Adapters
never put provider response bodies, API keys or prompts into error messages.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal, Protocol

# --- cancellation ---------------------------------------------------------------------------------------


class CancellationToken:
    """Stops a generation or an embedding batch (§5). Shared between the caller and the adapter."""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def cancel(self) -> None:
        self._event.set()

    @property
    def cancelled(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:
        await self._event.wait()

    def raise_if_cancelled(self) -> None:
        if self._event.is_set():
            raise ProviderCancelledError


# --- errors ---------------------------------------------------------------------------------------------


class ProviderErrorCode(StrEnum):
    AUTH_FAILED = "provider_auth_failed"
    EGRESS_DENIED = "egress_denied"
    RATE_LIMITED = "provider_rate_limited"
    TIMEOUT = "provider_timeout"
    UNAVAILABLE = "provider_unavailable"
    BAD_REQUEST = "provider_bad_request"
    MODEL_NOT_FOUND = "provider_model_not_found"
    CONTENT_FILTERED = "provider_content_filtered"
    INVALID_RESPONSE = "provider_invalid_response"
    CANCELLED = "provider_cancelled"


class ProviderError(Exception):
    """Base of typed provider errors. ``message`` is safe to show and never contains provider output."""

    code: ProviderErrorCode = ProviderErrorCode.UNAVAILABLE
    default_message = "The provider is unavailable."
    retryable = False

    def __init__(self, message: str | None = None, *, retry_after: float | None = None) -> None:
        super().__init__(message or self.default_message)
        self.message = message or self.default_message
        self.retry_after = retry_after


class ProviderAuthError(ProviderError):
    code = ProviderErrorCode.AUTH_FAILED
    default_message = "The provider rejected the credentials."


class ProviderEgressDeniedError(ProviderError):
    code = ProviderErrorCode.EGRESS_DENIED
    default_message = "Endpoint is not allowed by instance network policy."


class ProviderRateLimitedError(ProviderError):
    code = ProviderErrorCode.RATE_LIMITED
    default_message = "The provider is rate limiting requests."
    retryable = True


class ProviderTimeoutError(ProviderError):
    code = ProviderErrorCode.TIMEOUT
    default_message = "The provider did not respond in time."
    retryable = True


class ProviderUnavailableError(ProviderError):
    code = ProviderErrorCode.UNAVAILABLE
    default_message = "The provider is unavailable."
    retryable = True


class ProviderBadRequestError(ProviderError):
    code = ProviderErrorCode.BAD_REQUEST
    default_message = "The provider rejected the request."


class ProviderModelNotFoundError(ProviderError):
    code = ProviderErrorCode.MODEL_NOT_FOUND
    default_message = "The model is not available at this provider."


class ProviderContentFilteredError(ProviderError):
    code = ProviderErrorCode.CONTENT_FILTERED
    default_message = "The provider declined to generate a response."


class ProviderInvalidResponseError(ProviderError):
    code = ProviderErrorCode.INVALID_RESPONSE
    default_message = "The provider returned an unexpected response."


class ProviderCancelledError(ProviderError):
    code = ProviderErrorCode.CANCELLED
    default_message = "The operation was cancelled."


# --- capabilities and connection tests ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LLMCapabilities:
    streaming: bool = True
    tools: bool = False  # tools are not used in the MVP (§8)
    max_context_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class EmbeddingCapabilities:
    max_batch: int
    dimensions: int | None = None  # model dependent; checked by the embedding profile test


@dataclass(frozen=True, slots=True)
class ConnectionTestResult:
    ok: bool
    latency_ms: int
    models: tuple[str, ...] = ()  # model ids the provider reports, when it lists them
    error: ProviderError | None = None


# --- chat -----------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True, slots=True)
class ContextDocument:
    """A retrieved fragment. Untrusted data: adapters keep it apart from the instructions (§8)."""

    id: str  # citation id issued by the backend
    text: str
    title: str | None = None


@dataclass(frozen=True, slots=True)
class ChatRequest:
    model_id: str
    instructions: str
    messages: tuple[ChatMessage, ...]
    context: tuple[ContextDocument, ...] = ()
    max_output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class TextDelta:
    text: str


@dataclass(frozen=True, slots=True)
class UsageReport:
    input_tokens: int | None = None
    output_tokens: int | None = None


class FinishReason(StrEnum):
    STOP = "stop"
    LENGTH = "length"
    CONTENT_FILTER = "content_filter"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class Finished:
    reason: FinishReason
    usage: UsageReport = field(default_factory=UsageReport)


type ChatStreamEvent = TextDelta | Finished


class LLMProvider(Protocol):
    def capabilities(self) -> LLMCapabilities: ...

    async def test_connection(self, *, timeout: float) -> ConnectionTestResult:  # noqa: ASYNC109
        """Never raises: failures come back as ``ok=False`` with a typed error."""
        ...

    def stream_chat(
        self,
        request: ChatRequest,
        *,
        cancel: CancellationToken,
        timeout: float,
    ) -> AsyncIterator[ChatStreamEvent]:
        """Text deltas, then exactly one :class:`Finished`. Raises :class:`ProviderError`."""
        ...


class EmbeddingProvider(Protocol):
    def embedding_capabilities(self) -> EmbeddingCapabilities: ...

    async def test_connection(self, *, timeout: float) -> ConnectionTestResult: ...  # noqa: ASYNC109

    async def embed(
        self, texts: Sequence[str], *, model_id: str, cancel: CancellationToken
    ) -> list[list[float]]:
        """One vector per text, in order. Raises :class:`ProviderError`."""
        ...
