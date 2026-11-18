"""Builds the adapter for a configured provider. The only place that maps ``ProviderType`` to a class."""

from __future__ import annotations

from typing import Final

from plummy.application.ai.ports import EmbeddingProvider, LLMProvider
from plummy.core.network.policy import EgressPurpose
from plummy.domain.ai import ProviderType
from plummy.infrastructure.http.safe_client import SafeHttpClient
from plummy.infrastructure.integrations.ai.anthropic import ANTHROPIC_BASE_URL, AnthropicProvider
from plummy.infrastructure.integrations.ai.common import ProviderConnection, RetryPolicy
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

EMBEDDING_PROVIDER_TYPES: Final = frozenset(
    {ProviderType.OPENAI, ProviderType.OPENAI_COMPATIBLE, ProviderType.OLLAMA}
)


class UnsupportedCapabilityError(ValueError):
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
