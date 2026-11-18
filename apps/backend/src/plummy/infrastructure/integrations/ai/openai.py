"""OpenAI (``/v1/models``, ``/v1/chat/completions`` SSE, ``/v1/embeddings``) and OpenAI-compatible servers.

OpenAI-compatible servers (vLLM, LiteLLM, LM Studio, ...) get the conservative dialect: ``max_tokens`` instead
of ``max_completion_tokens``, no ``stream_options``, and an optional API key.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any, Final

from plummy.application.ai.ports import (
    CancellationToken,
    ChatRequest,
    ChatStreamEvent,
    ConnectionTestResult,
    EmbeddingCapabilities,
    Finished,
    FinishReason,
    LLMCapabilities,
    ProviderAuthError,
    ProviderBadRequestError,
    ProviderConnection,
    ProviderContentFilteredError,
    ProviderError,
    ProviderInvalidResponseError,
    ProviderModelNotFoundError,
    ProviderRateLimitedError,
    ProviderUnavailableError,
    TextDelta,
    UsageReport,
)
from plummy.core.network.policy import EgressPurpose
from plummy.infrastructure.http.safe_client import SafeHttpClient
from plummy.infrastructure.integrations.ai.common import (
    MAX_EMBEDDING_RESPONSE_BYTES,
    NO_RETRY,
    ProviderHttp,
    RetryPolicy,
    chunked,
    iter_sse,
    measure_connection,
    model_ids,
    optional_int,
    parse_json_object,
    plain_messages,
    race,
    vectors,
)

OPENAI_BASE_URL: Final = "https://api.openai.com/v1"

_FINISH_REASONS: Final = {
    "stop": FinishReason.STOP,
    "length": FinishReason.LENGTH,
    "content_filter": FinishReason.CONTENT_FILTER,
}


def _stream_error(payload: dict[str, Any]) -> ProviderError:
    """An ``{"error": {...}}`` object sent inside the stream. Its message is not passed on."""
    error = payload.get("error")
    kind = error.get("type") or error.get("code") if isinstance(error, dict) else None
    match kind:
        case "invalid_api_key" | "authentication_error" | "permission_error":
            return ProviderAuthError()
        case "rate_limit_exceeded" | "rate_limit_error" | "insufficient_quota":
            return ProviderRateLimitedError()
        case "model_not_found":
            return ProviderModelNotFoundError()
        case "invalid_request_error" | "context_length_exceeded":
            return ProviderBadRequestError()
        case "content_filter":
            return ProviderContentFilteredError()
        case _:
            return ProviderUnavailableError()


class OpenAIProvider:
    """``LLMProvider`` and ``EmbeddingProvider`` for the OpenAI API."""

    compatible = False
    max_embedding_batch = 256

    def __init__(
        self,
        client: SafeHttpClient,
        connection: ProviderConnection,
        *,
        test_purpose: EgressPurpose = EgressPurpose.LLM,
        retry: RetryPolicy | None = None,
    ) -> None:
        headers = {"Accept": "application/json"}
        if connection.api_key is not None:
            headers["Authorization"] = f"Bearer {connection.api_key.reveal()}"
        self._http = ProviderHttp(client, connection, headers, retry or RetryPolicy())
        self._test_purpose = test_purpose

    def capabilities(self) -> LLMCapabilities:
        return LLMCapabilities(streaming=True)

    def embedding_capabilities(self) -> EmbeddingCapabilities:
        return EmbeddingCapabilities(max_batch=self.max_embedding_batch)

    async def test_connection(self, *, timeout: float) -> ConnectionTestResult:  # noqa: ASYNC109
        async def probe() -> tuple[str, ...]:
            body = await self._http.json(
                "GET", "/models", purpose=self._test_purpose, timeout=timeout, retry=NO_RETRY
            )
            if not isinstance(body, dict):
                raise ProviderInvalidResponseError
            return model_ids(body.get("data"), "id")

        return await measure_connection(probe, timeout=timeout, provider_type=self._http.provider_type)

    def _chat_body(self, request: ChatRequest) -> dict[str, Any]:
        messages: list[dict[str, str]] = [{"role": "system", "content": request.instructions}]
        messages += plain_messages(request)
        body: dict[str, Any] = {"model": request.model_id, "messages": messages, "stream": True}
        if request.max_output_tokens is not None:
            body["max_tokens" if self.compatible else "max_completion_tokens"] = request.max_output_tokens
        if not self.compatible:
            body["stream_options"] = {"include_usage": True}
        return body

    async def stream_chat(
        self,
        request: ChatRequest,
        *,
        cancel: CancellationToken,
        timeout: float,  # noqa: ASYNC109
    ) -> AsyncIterator[ChatStreamEvent]:
        body = self._chat_body(request)
        reason: FinishReason | None = None
        usage = UsageReport()
        async with self._http.stream(
            "/chat/completions", purpose=EgressPurpose.LLM, timeout=timeout, body=body, cancel=cancel
        ) as lines:
            async for event in iter_sse(lines):
                if event.data.strip() == "[DONE]":
                    break
                payload = parse_json_object(event.data)
                if "error" in payload:
                    raise _stream_error(payload)
                if isinstance(payload.get("usage"), dict):
                    usage = UsageReport(
                        input_tokens=optional_int(payload["usage"].get("prompt_tokens")),
                        output_tokens=optional_int(payload["usage"].get("completion_tokens")),
                    )
                choices = payload.get("choices")
                if not isinstance(choices, list) or not choices:
                    continue
                choice = choices[0]
                if not isinstance(choice, dict):
                    raise ProviderInvalidResponseError
                delta = choice.get("delta")
                text = delta.get("content") if isinstance(delta, dict) else None
                if isinstance(text, str) and text:
                    yield TextDelta(text)
                finish = choice.get("finish_reason")
                if isinstance(finish, str):
                    reason = _FINISH_REASONS.get(finish, FinishReason.OTHER)
        if reason is None:
            raise ProviderInvalidResponseError("The provider stream ended unexpectedly.")
        yield Finished(reason, usage)

    async def embed(
        self, texts: Sequence[str], *, model_id: str, cancel: CancellationToken
    ) -> list[list[float]]:
        result: list[list[float]] = []
        for batch in chunked(texts, self.max_embedding_batch):
            body = await race(
                cancel,
                self._http.json(
                    "POST",
                    "/embeddings",
                    purpose=EgressPurpose.EMBEDDING,
                    timeout=120.0,
                    body={"model": model_id, "input": list(batch), "encoding_format": "float"},
                    model_scoped=True,
                    max_response_bytes=MAX_EMBEDDING_RESPONSE_BYTES,
                ),
            )
            data = body.get("data") if isinstance(body, dict) else None
            if not isinstance(data, list) or not all(
                isinstance(item, dict) and isinstance(item.get("index"), int) for item in data
            ):
                raise ProviderInvalidResponseError
            ordered = sorted(data, key=lambda item: item["index"])
            if [item["index"] for item in ordered] != list(range(len(batch))):
                raise ProviderInvalidResponseError
            result += vectors([item.get("embedding") for item in ordered], len(batch))
        return result


class OpenAICompatibleProvider(OpenAIProvider):
    compatible = True
    max_embedding_batch = 64
