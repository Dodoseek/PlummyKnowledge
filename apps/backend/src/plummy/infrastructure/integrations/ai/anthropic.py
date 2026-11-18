"""Anthropic Messages API on raw HTTP: ``/v1/models``, ``/v1/messages`` with SSE streaming.

Pinned to ``anthropic-version: 2026-10-21``. With this version sampling parameters (``temperature``,
``top_p``, ``top_k``) are rejected, so none are sent. Anthropic has no embeddings endpoint.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Final

from plummy.application.ai.ports import (
    CancellationToken,
    ChatRequest,
    ChatStreamEvent,
    ConnectionTestResult,
    Finished,
    FinishReason,
    LLMCapabilities,
    ProviderAuthError,
    ProviderBadRequestError,
    ProviderConnection,
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
    NO_RETRY,
    ProviderHttp,
    RetryPolicy,
    check_chat_request,
    iter_sse,
    measure_connection,
    model_ids,
    optional_int,
    parse_json_object,
    render_context,
)

ANTHROPIC_BASE_URL: Final = "https://api.anthropic.com"
ANTHROPIC_VERSION: Final = "2026-10-21"
DEFAULT_MAX_OUTPUT_TOKENS: Final = 4096  # required by the API

_STOP_REASONS: Final = {
    "end_turn": FinishReason.STOP,
    "stop_sequence": FinishReason.STOP,
    "max_tokens": FinishReason.LENGTH,
    "refusal": FinishReason.CONTENT_FILTER,
}


def _stream_error(payload: dict[str, Any]) -> ProviderError:
    error = payload.get("error")
    match error.get("type") if isinstance(error, dict) else None:
        case "authentication_error" | "permission_error":
            return ProviderAuthError()
        case "rate_limit_error":
            return ProviderRateLimitedError()
        case "not_found_error":
            return ProviderModelNotFoundError()
        case "invalid_request_error" | "request_too_large":
            return ProviderBadRequestError()
        case _:  # overloaded_error, api_error, unknown
            return ProviderUnavailableError()


class AnthropicProvider:
    def __init__(
        self,
        client: SafeHttpClient,
        connection: ProviderConnection,
        *,
        retry: RetryPolicy | None = None,
    ) -> None:
        headers = {"Accept": "application/json", "anthropic-version": ANTHROPIC_VERSION}
        if connection.api_key is not None:
            headers["x-api-key"] = connection.api_key.reveal()
        self._http = ProviderHttp(client, connection, headers, retry or RetryPolicy())

    def capabilities(self) -> LLMCapabilities:
        return LLMCapabilities(streaming=True)

    async def test_connection(self, *, timeout: float) -> ConnectionTestResult:  # noqa: ASYNC109
        async def probe() -> tuple[str, ...]:
            body = await self._http.json(
                "GET", "/v1/models?limit=1000", purpose=EgressPurpose.LLM, timeout=timeout, retry=NO_RETRY
            )
            if not isinstance(body, dict):
                raise ProviderInvalidResponseError
            return model_ids(body.get("data"), "id")

        return await measure_connection(probe, timeout=timeout, provider_type=self._http.provider_type)

    @staticmethod
    def _body(request: ChatRequest) -> dict[str, Any]:
        check_chat_request(request)
        messages: list[dict[str, Any]] = [
            {"role": message.role, "content": message.content} for message in request.messages
        ]
        if request.context:  # its own content block, ahead of the question
            question = request.messages[-1].content
            messages[-1]["content"] = [
                {"type": "text", "text": render_context(request.context)},
                {"type": "text", "text": question},
            ]
        return {
            "model": request.model_id,
            "max_tokens": request.max_output_tokens or DEFAULT_MAX_OUTPUT_TOKENS,
            "system": request.instructions,
            "messages": messages,
            "stream": True,
        }

    async def stream_chat(
        self,
        request: ChatRequest,
        *,
        cancel: CancellationToken,
        timeout: float,  # noqa: ASYNC109
    ) -> AsyncIterator[ChatStreamEvent]:
        body = self._body(request)
        input_tokens: int | None = None
        output_tokens: int | None = None
        reason: FinishReason | None = None
        stopped = False
        async with self._http.stream(
            "/v1/messages", purpose=EgressPurpose.LLM, timeout=timeout, body=body, cancel=cancel
        ) as lines:
            async for event in iter_sse(lines):
                if event.event == "ping":
                    continue
                payload = parse_json_object(event.data)
                kind = payload.get("type", event.event)
                if kind == "error":
                    raise _stream_error(payload)
                if kind == "message_start":
                    message = payload.get("message")
                    usage = message.get("usage") if isinstance(message, dict) else None
                    if isinstance(usage, dict):
                        input_tokens = optional_int(usage.get("input_tokens"))
                elif kind == "content_block_delta":
                    delta = payload.get("delta")
                    if isinstance(delta, dict) and delta.get("type") == "text_delta":
                        text = delta.get("text")
                        if isinstance(text, str) and text:
                            yield TextDelta(text)
                elif kind == "message_delta":
                    delta, usage = payload.get("delta"), payload.get("usage")
                    stop = delta.get("stop_reason") if isinstance(delta, dict) else None
                    if isinstance(stop, str):
                        reason = _STOP_REASONS.get(stop, FinishReason.OTHER)
                    if isinstance(usage, dict):
                        output_tokens = optional_int(usage.get("output_tokens"))
                elif kind == "message_stop":
                    stopped = True
                    break
        if not stopped:
            raise ProviderInvalidResponseError("The provider stream ended unexpectedly.")
        yield Finished(reason or FinishReason.OTHER, UsageReport(input_tokens, output_tokens))
