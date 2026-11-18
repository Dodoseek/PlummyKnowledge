"""Live checks of the adapters against the real vendor APIs (public endpoints, default network policy).

Never run in CI: they need ``LIVE_PROVIDER_TESTS=1`` plus the vendor key. They cost a few tokens each.

    LIVE_PROVIDER_TESTS=1 ANTHROPIC_API_KEY=... OPENAI_API_KEY=... OPENAI_TEST_MODEL=... pytest -m live
"""

from __future__ import annotations

import dataclasses
import os
from collections.abc import AsyncIterator

import pytest

from plummy.application.ai.ports import (
    CancellationToken,
    ChatMessage,
    ChatRequest,
    ContextDocument,
    Finished,
    ProviderConnection,
    TextDelta,
)
from plummy.core.network.policy import EgressPolicy
from plummy.domain.ai import ProviderType
from plummy.domain.secrets import SecretValue
from plummy.infrastructure.http.safe_client import SafeHttpClient
from plummy.infrastructure.integrations.ai.factory import (
    DEFAULT_BASE_URLS,
    build_embedding_provider,
    build_llm_provider,
)

pytestmark = [
    pytest.mark.live,
    pytest.mark.skipif(os.environ.get("LIVE_PROVIDER_TESTS") != "1", reason="LIVE_PROVIDER_TESTS=1 not set"),
]

REQUEST = ChatRequest(
    model_id="",
    instructions="Answer in one short sentence using only the documents.",
    messages=(ChatMessage("user", "How many vacation days are there?"),),
    context=(ContextDocument("c1", "Employees get 28 vacation days per year.", title="HR policy"),),
    max_output_tokens=64,
)


@pytest.fixture
async def client() -> AsyncIterator[SafeHttpClient]:
    http = SafeHttpClient(EgressPolicy())  # public endpoints only, as in production
    yield http
    await http.aclose()


def connection(provider_type: ProviderType, key_env: str) -> ProviderConnection:
    key = os.environ.get(key_env)
    if not key:
        pytest.skip(f"{key_env} not set")
    return ProviderConnection(provider_type, DEFAULT_BASE_URLS[provider_type], SecretValue(key))


async def answer(client: SafeHttpClient, conn: ProviderConnection, model_id: str) -> str:
    provider = build_llm_provider(client, conn)
    request = dataclasses.replace(REQUEST, model_id=model_id)
    events = [event async for event in provider.stream_chat(request, cancel=CancellationToken(), timeout=60)]
    assert isinstance(events[-1], Finished)
    return "".join(event.text for event in events if isinstance(event, TextDelta))


async def test_anthropic_live(client: SafeHttpClient) -> None:
    conn = connection(ProviderType.ANTHROPIC, "ANTHROPIC_API_KEY")
    result = await build_llm_provider(client, conn).test_connection(timeout=30)
    assert result.ok, result.error
    model = os.environ.get("ANTHROPIC_TEST_MODEL", "claude-haiku-5-5")
    assert "28" in await answer(client, conn, model)


async def test_openai_live(client: SafeHttpClient) -> None:
    conn = connection(ProviderType.OPENAI, "OPENAI_API_KEY")
    result = await build_llm_provider(client, conn).test_connection(timeout=30)
    assert result.ok, result.error
    model = os.environ.get("OPENAI_TEST_MODEL")
    if model:
        assert "28" in await answer(client, conn, model)
    embedding_model = os.environ.get("OPENAI_TEST_EMBEDDING_MODEL", "text-embedding-3-small")
    vectors = await build_embedding_provider(client, conn).embed(
        ["vacation days", "expense policy"], model_id=embedding_model, cancel=CancellationToken()
    )
    assert len(vectors) == 2
    assert len(vectors[0]) == len(vectors[1]) > 0
