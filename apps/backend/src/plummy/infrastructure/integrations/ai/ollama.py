"""Ollama: ``/api/tags``, ``/api/chat`` (NDJSON stream), ``/api/embed``.

A local server is reachable only through an allowlist entry in Instance Settings (ADR-007).
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
    ProviderConnection,
    ProviderInvalidResponseError,
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
    measure_connection,
    model_ids,
    optional_int,
    parse_json_object,
    plain_messages,
    race,
    vectors,
)

_DONE_REASONS: Final = {"stop": FinishReason.STOP, "length": FinishReason.LENGTH}


class OllamaProvider:
    max_embedding_batch = 64

    def __init__(
        self,
        client: SafeHttpClient,
        connection: ProviderConnection,
        *,
        test_purpose: EgressPurpose = EgressPurpose.LLM,
        retry: RetryPolicy | None = None,
    ) -> None:
        headers = {"Accept": "application/json"}
        if connection.api_key is not None:  # e.g. behind an authenticating reverse proxy
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
                "GET", "/api/tags", purpose=self._test_purpose, timeout=timeout, retry=NO_RETRY
            )
            if not isinstance(body, dict):
                raise ProviderInvalidResponseError
            return model_ids(body.get("models"), "name")

        return await measure_connection(probe, timeout=timeout, provider_type=self._http.provider_type)

    async def stream_chat(
        self,
        request: ChatRequest,
        *,
        cancel: CancellationToken,
        timeout: float,  # noqa: ASYNC109
    ) -> AsyncIterator[ChatStreamEvent]:
        messages: list[dict[str, str]] = [{"role": "system", "content": request.instructions}]
        messages += plain_messages(request)
        body: dict[str, Any] = {"model": request.model_id, "messages": messages, "stream": True}
        if request.max_output_tokens is not None:
            body["options"] = {"num_predict": request.max_output_tokens}
        finished: Finished | None = None
        async with self._http.stream(
            "/api/chat", purpose=EgressPurpose.LLM, timeout=timeout, body=body, cancel=cancel
        ) as lines:
            async for line in lines:
                if not line.strip():
                    continue
                payload = parse_json_object(line)
                if "error" in payload:
                    raise ProviderUnavailableError
                message = payload.get("message")
                text = message.get("content") if isinstance(message, dict) else None
                if isinstance(text, str) and text:
                    yield TextDelta(text)
                if payload.get("done") is True:
                    reason = payload.get("done_reason")
                    finished = Finished(
                        _DONE_REASONS.get(reason, FinishReason.OTHER)
                        if isinstance(reason, str)
                        else FinishReason.STOP,
                        UsageReport(
                            input_tokens=optional_int(payload.get("prompt_eval_count")),
                            output_tokens=optional_int(payload.get("eval_count")),
                        ),
                    )
                    break
        if finished is None:
            raise ProviderInvalidResponseError("The provider stream ended unexpectedly.")
        yield finished

    async def embed(
        self, texts: Sequence[str], *, model_id: str, cancel: CancellationToken
    ) -> list[list[float]]:
        result: list[list[float]] = []
        for batch in chunked(texts, self.max_embedding_batch):
            body = await race(
                cancel,
                self._http.json(
                    "POST",
                    "/api/embed",
                    purpose=EgressPurpose.EMBEDDING,
                    timeout=300.0,
                    body={"model": model_id, "input": list(batch)},
                    model_scoped=True,
                    max_response_bytes=MAX_EMBEDDING_RESPONSE_BYTES,
                ),
            )
            if not isinstance(body, dict):
                raise ProviderInvalidResponseError
            result += vectors(body.get("embeddings"), len(batch))
        return result
