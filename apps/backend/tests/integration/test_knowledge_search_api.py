"""``POST /api/v1/knowledge-bases/{id}/search`` (API_CONTRACTS.md §2.10): real app, PostgreSQL and Redis.

Only the provider gateway of the running app is replaced by the deterministic fake embedder; the router,
session, access policy, rate limiter and SQL are the production ones.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from plummy.application.ai.ports import ProviderAuthError, ProviderTimeoutError
from plummy.application.retrieval import use_cases as retrieval_use_cases
from tests.integration.harness import running_auth_app
from tests.integration.test_document_upload import Env, make_env
from tests.integration.test_indexing import FakeGateway
from tests.integration.test_knowledge_bases_api import signed_in
from tests.integration.test_retrieval import EXPENSES, LEAVE, TextEmbedder, grant, indexed

pytestmark = pytest.mark.integration


@pytest.fixture
async def env(
    migrated_database_url: str, redis_url: str, clean_rate_limits: None, tmp_path: Path
) -> AsyncIterator[Env]:
    async with running_auth_app(migrated_database_url, redis_url, tmp_path / "bootstrap") as harness:
        yield await make_env(harness)


@pytest.fixture
async def embedder(env: Env, monkeypatch: pytest.MonkeyPatch) -> TextEmbedder:
    fake = TextEmbedder()
    retrieve = env.harness.resources.knowledge.search._retrieve
    monkeypatch.setattr(retrieve, "_gateway", FakeGateway(fake))
    await indexed(env, env.kb_id, "leave.md", LEAVE, fake)
    await indexed(env, env.kb_id, "expenses.md", EXPENSES, fake)
    return fake


def search_url(kb_id: str) -> str:
    return f"/api/v1/knowledge-bases/{kb_id}/search"


async def test_search_returns_ranked_chunks_with_citation_metadata(env: Env, embedder: TextEmbedder) -> None:
    response = await env.admin.post(search_url(env.kb_id), json={"query": "отпуск сотрудника"})
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["mode"] == "hybrid"
    assert body["keyword_query_empty"] is False
    first = body["items"][0]
    assert first["filename"] == "leave.md"
    assert first["keyword_rank"] is not None
    assert first["vector_rank"] is not None
    assert any(i["filename"] == "leave.md" and i["keyword_rank"] == 1 for i in body["items"])
    assert set(first) == {
        "chunk_id", "document_id", "document_version_id", "filename", "chunk_index", "content", "token_count",
        "page_start", "page_end", "section_path", "sheet_name", "row_start", "row_end", "score",
        "vector_rank", "keyword_rank",
    }  # fmt: skip
    assert body["total_tokens"] == sum(item["token_count"] for item in body["items"])
    assert [i["score"] for i in body["items"]] == sorted((i["score"] for i in body["items"]), reverse=True)


@pytest.mark.parametrize(("mode", "branch"), [("keyword", "vector_rank"), ("vector", "keyword_rank")])
async def test_single_branch_modes(env: Env, embedder: TextEmbedder, mode: str, branch: str) -> None:
    response = await env.admin.post(search_url(env.kb_id), json={"query": "hotel costs", "mode": mode})
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert items
    assert all(item[branch] is None for item in items)


async def test_top_k_limits_the_items(env: Env, embedder: TextEmbedder) -> None:
    response = await env.admin.post(search_url(env.kb_id), json={"query": "отпуск hotel", "top_k": 1})
    assert len(response.json()["items"]) == 1


async def test_without_access_the_knowledge_base_does_not_exist(env: Env, embedder: TextEmbedder) -> None:
    member, user_id = await signed_in(env, "member@example.com", [])
    calls = len(embedder.calls)
    response = await member.post(search_url(env.kb_id), json={"query": "отпуск"})
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    assert len(embedder.calls) == calls  # refused before the provider
    await grant(env, env.kb_id, user_id)
    allowed = await member.post(search_url(env.kb_id), json={"query": "отпуск"})
    assert allowed.status_code == 200
    assert allowed.json()["items"]


async def test_unauthenticated_search_is_rejected(env: Env, embedder: TextEmbedder) -> None:
    async with env.harness.new_client() as anonymous:
        response = await anonymous.post(search_url(env.kb_id), json={"query": "отпуск"})
    assert response.status_code == 401


async def test_unknown_knowledge_base(env: Env, embedder: TextEmbedder) -> None:
    response = await env.admin.post(search_url("00000000-0000-0000-0000-000000000001"), json={"query": "x"})
    assert response.status_code == 404


@pytest.mark.parametrize(
    ("body", "field", "code"),
    [
        ({"query": " \n\t "}, "body.query", "empty_query"),
        ({"query": "x " * 600}, "body.query", "query_too_long"),
        ({"query": ""}, "body.query", None),
        ({"query": "x", "top_k": 0}, "body.top_k", None),
        ({"query": "x", "top_k": 51}, "body.top_k", None),
        ({"query": "x", "mode": "semantic"}, "body.mode", None),
        ({"query": "x", "knowledge_base_ids": ["a"]}, "body.knowledge_base_ids", None),
    ],
)
async def test_invalid_requests(
    env: Env, embedder: TextEmbedder, body: dict[str, Any], field: str, code: str | None
) -> None:
    calls = len(embedder.calls)
    response = await env.admin.post(search_url(env.kb_id), json=body)
    assert response.status_code == 422, response.text
    errors = response.json()["errors"]
    assert errors[0]["field"] == field
    if code is not None:
        assert errors[0]["code"] == code
    assert len(embedder.calls) == calls


async def test_search_is_rate_limited_per_user(
    env: Env, embedder: TextEmbedder, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(retrieval_use_cases, "SEARCHES_PER_MINUTE", 2)
    for _ in range(2):
        assert (await env.admin.post(search_url(env.kb_id), json={"query": "отпуск"})).status_code == 200
    limited = await env.admin.post(search_url(env.kb_id), json={"query": "отпуск"})
    assert limited.status_code == 429
    assert limited.json()["code"] == "rate_limited"
    assert int(limited.headers["retry-after"]) > 0


@pytest.mark.parametrize(
    ("failure", "status", "code"),
    [(ProviderAuthError(), 502, "provider_error"), (ProviderTimeoutError(), 504, "provider_timeout")],
)
async def test_provider_failures_carry_no_provider_output(
    env: Env, embedder: TextEmbedder, failure: Exception, status: int, code: str
) -> None:
    embedder.failures[len(embedder.calls)] = failure  # type: ignore[assignment]
    response = await env.admin.post(search_url(env.kb_id), json={"query": "отпуск"})
    assert response.status_code == status
    body = response.json()
    assert body["code"] == code
    assert "detail" not in body


async def test_disabled_provider_makes_search_by_meaning_unavailable(
    env: Env, embedder: TextEmbedder
) -> None:
    await env.harness.sql("UPDATE ai_providers SET enabled = false")
    response = await env.admin.post(search_url(env.kb_id), json={"query": "отпуск"})
    assert response.status_code == 409
    assert response.json()["code"] == "embedding_unavailable"
    keyword = await env.admin.post(search_url(env.kb_id), json={"query": "отпуск", "mode": "keyword"})
    assert keyword.status_code == 200
    assert keyword.json()["items"]
