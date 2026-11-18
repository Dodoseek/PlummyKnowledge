"""Builds the adapter for a configured provider. The only place that maps ``ProviderType`` to a class."""

from __future__ import annotations

import asyncio
from typing import Final

from plummy.application.ai.ports import (
    ConnectionTestResult,
    EmbeddingProvider,
    EndpointNotAllowed,
    InvalidEndpoint,
    LLMProvider,
    ProviderConnection,
    UnsupportedCapability,
)
from plummy.core.network.addresses import IPAddress
from plummy.core.network.errors import EgressDenialReason, EgressDeniedError
from plummy.core.network.policy import EgressPurpose
from plummy.core.network.urls import parse_egress_url
from plummy.domain.ai import EMBEDDING_CAPABLE, ProviderType
from plummy.infrastructure.http.safe_client import Resolver, SafeHttpClient, SystemResolver
from plummy.infrastructure.integrations.ai.anthropic import ANTHROPIC_BASE_URL, AnthropicProvider
from plummy.infrastructure.integrations.ai.common import RetryPolicy
from plummy.infrastructure.integrations.ai.ollama import OllamaProvider
from plummy.infrastructure.integrations.ai.openai import (
    OPENAI_BASE_URL,
    OpenAICompatibleProvider,
    OpenAIProvider,
)

DEFAULT_BASE_URLS: Final[dict[ProviderType, str]] = {
    ProviderType.OPENAI: OPENAI_BASE_URL,
    ProviderType.ANTHROPIC: ANTHROPIC_BASE_URL,
}
"""Providers without a default (Ollama, OpenAI-compatible) need an explicit base URL."""

EMBEDDING_PROVIDER_TYPES: Final = EMBEDDING_CAPABLE
RESOLVE_TIMEOUT_SECONDS: Final = 5.0


class UnsupportedCapabilityError(UnsupportedCapability):
    code = "capability_not_supported"


def build_llm_provider(
    client: SafeHttpClient, connection: ProviderConnection, *, retry: RetryPolicy | None = None
) -> LLMProvider:
    match connection.provider_type:
        case ProviderType.OPENAI:
            return OpenAIProvider(client, connection, retry=retry)
        case ProviderType.OPENAI_COMPATIBLE:
            return OpenAICompatibleProvider(client, connection, retry=retry)
        case ProviderType.ANTHROPIC:
            return AnthropicProvider(client, connection, retry=retry)
        case ProviderType.OLLAMA:
            return OllamaProvider(client, connection, retry=retry)


def build_embedding_provider(
    client: SafeHttpClient, connection: ProviderConnection, *, retry: RetryPolicy | None = None
) -> EmbeddingProvider:
    purpose = EgressPurpose.EMBEDDING
    match connection.provider_type:
        case ProviderType.OPENAI:
            return OpenAIProvider(client, connection, test_purpose=purpose, retry=retry)
        case ProviderType.OPENAI_COMPATIBLE:
            return OpenAICompatibleProvider(client, connection, test_purpose=purpose, retry=retry)
        case ProviderType.OLLAMA:
            return OllamaProvider(client, connection, test_purpose=purpose, retry=retry)
        case ProviderType.ANTHROPIC:
            raise UnsupportedCapabilityError("Anthropic does not provide an embeddings API.")


class HttpProviderGateway:
    """``ProviderGateway`` over ``SafeHttpClient``: every adapter connection passes the egress policy."""

    def __init__(self, client: SafeHttpClient, *, resolver: Resolver | None = None) -> None:
        self._client = client
        self._resolver = resolver or SystemResolver()

    def default_base_url(self, provider_type: ProviderType) -> str | None:
        return DEFAULT_BASE_URLS.get(provider_type)

    async def check_endpoint(self, base_url: str, provider_type: ProviderType) -> frozenset[EgressPurpose]:
        if "?" in base_url or "#" in base_url:
            raise InvalidEndpoint
        try:
            url = parse_egress_url(base_url)
        except EgressDeniedError:
            raise InvalidEndpoint from None
        policy = self._client.policy
        purposes = [EgressPurpose.LLM]
        if provider_type in EMBEDDING_PROVIDER_TYPES:
            purposes.append(EgressPurpose.EMBEDDING)
        addresses: list[IPAddress] | None = None
        allowed: set[EgressPurpose] = set()
        reasons: list[EgressDenialReason] = []
        for purpose in purposes:
            try:
                rule = policy.rule_for(url, purpose, str(provider_type))
                if url.address is None:
                    if addresses is None:
                        addresses = await self._resolve(url.host, url.port)
                    policy.check_addresses(rule, addresses)
            except EgressDeniedError as exc:
                reasons.append(exc.reason)
                continue
            allowed.add(purpose)
        if not allowed:
            raise EndpointNotAllowed(reasons[0].value)
        return frozenset(allowed)

    async def _resolve(self, host: str, port: int) -> list[IPAddress]:
        """An unresolvable name passes here: the connection test reports it; connections re-check anyway."""
        try:
            async with asyncio.timeout(RESOLVE_TIMEOUT_SECONDS):
                return await self._resolver.resolve(host, port)
        except (OSError, TimeoutError):
            return []

    def llm(self, connection: ProviderConnection) -> LLMProvider:
        return build_llm_provider(self._client, connection)

    def embedding(self, connection: ProviderConnection) -> EmbeddingProvider:
        return build_embedding_provider(self._client, connection)

    async def test_connection(
        self,
        connection: ProviderConnection,
        *,
        purpose: EgressPurpose,
        timeout: float,  # noqa: ASYNC109
    ) -> ConnectionTestResult:
        if purpose is EgressPurpose.EMBEDDING:
            return await self.embedding(connection).test_connection(timeout=timeout)
        return await self.llm(connection).test_connection(timeout=timeout)
