"""Hybrid retrieval on real PostgreSQL + pgvector (ARCHITECTURE.md §9.4, DATABASE_SCHEMA.md §5.11).

Documents go through the real ingestion and index phases; vectors come from the deterministic
``FakeEmbedder`` (a query equal to a chunk's text gets that chunk's vector). Access is the real policy:
grants are rows in ``knowledge_base_access``, the security context names a real user.
"""

from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from plummy.application.ai.ports import CancellationToken, ProviderAuthError, ProviderTimeoutError
from plummy.application.errors import (
    InvalidInputError,
    NotFoundError,
    UpstreamProviderError,
    UpstreamTimeoutError,
)
from plummy.application.knowledge.vector_index import EnsureVectorIndex
from plummy.application.retrieval.model import RetrievalResult, RetrievalSettings, SearchMode
from plummy.application.retrieval.use_cases import EmbeddingUnavailableError, RetrieveChunks
from plummy.application.security import SecurityContext
from plummy.infrastructure.db import retrieval as sql_retrieval
from plummy.infrastructure.db.engine import create_engine, create_session_factory
from plummy.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWorkFactory
from plummy.infrastructure.db.vector_index import PgVectorIndexManager
from tests.integration.harness import running_auth_app
from tests.integration.rbac import active_user
from tests.integration.test_document_upload import Env, make_env, run, upload
from tests.integration.test_indexing import FakeEmbedder, FakeGateway, index

pytestmark = pytest.mark.integration

LEAVE = "\n\n".join(
    [
        "# Отпуск",
        "Сотрудники получают 28 дней оплачиваемого отпуска в год.",
        "## Перенос",
        "До пяти дней отпуска можно перенести на следующий год.",
    ]
).encode()
EXPENSES = (
    b"# Expenses\n\nTravel expenses are reimbursed within 30 days.\n\n"
    b"## Hotels\n\nHotel costs up to 150 EUR per night are covered."
)
SECRET = b"# Salaries\n\nThe salary review happens in March for every employee."


@dataclass
class TextEmbedder(FakeEmbedder):
    """Hashes whitespace-normalized text: a query equal to a chunk (as normalized) gets its vector."""

    async def embed(
        self, texts: Sequence[str], *, model_id: str, cancel: CancellationToken
    ) -> list[list[float]]:
        number = len(self.calls)
        self.calls.append(list(texts))
        if number in self.failures:
            raise self.failures[number]
        return [
            [b / 255 + 0.01 for b in hashlib.sha256(" ".join(t.split()).encode()).digest()[: self.dimensions]]
            for t in texts
        ]


@pytest.fixture
async def env(
    migrated_database_url: str, redis_url: str, clean_rate_limits: None, tmp_path: Path
) -> AsyncIterator[Env]:
    async with running_auth_app(migrated_database_url, redis_url, tmp_path / "bootstrap") as harness:
        yield await make_env(harness)


def context(user_id: str | UUID, *permissions: str) -> SecurityContext:
    now = datetime.now(UTC)
    return SecurityContext(
        user_id=UUID(str(user_id)),
        session_id=uuid4(),
        email="user@example.com",
        display_name="User",
        roles=frozenset(),
        permissions=frozenset(permissions),
        session_expires_at=now + timedelta(hours=1),
        session_idle_expires_at=now + timedelta(hours=1),
    )


async def indexed(env: Env, kb_id: str, filename: str, data: bytes, embedder: FakeEmbedder) -> str:
    response = await upload(env.admin, kb_id, filename, data)
    assert response.status_code == 202, response.text
    job_id, document_id = response.json()["job"]["id"], response.json()["document"]["id"]
    await run(env, job_id)
    await index(env, job_id, embedder)
    [(status,)] = await env.harness.sql(
        "SELECT processing_status FROM documents WHERE id = :id", id=document_id
    )
    assert status == "indexed"
    return str(document_id)


async def create_kb(env: Env, name: str, profile_id: str | None = None) -> str:
    if profile_id is None:
        kb = (await env.admin.get(f"/api/v1/knowledge-bases/{env.kb_id}")).json()
        profile_id = kb["embedding_profile_id"]
    response = await env.admin.post(
        "/api/v1/knowledge-bases", json={"name": name, "embedding_profile_id": profile_id}
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def grant(env: Env, kb_id: str, user_id: str, level: str = "read") -> None:
    await env.harness.sql(
        "INSERT INTO knowledge_base_access (id, knowledge_base_id, principal_type, user_id, access_level) "
        "VALUES (gen_random_uuid(), :kb, 'user', :user_id, :level)",
        kb=kb_id,
        user_id=user_id,
        level=level,
    )


class Retriever:
    """``RetrieveChunks`` against the harness database with the fake embedder."""

    def __init__(self, env: Env, embedder: FakeEmbedder) -> None:
        self.env, self.embedder = env, embedder

    async def __call__(
        self,
        who: SecurityContext,
        kb_ids: list[str],
        query: str,
        *,
        mode: SearchMode = SearchMode.HYBRID,
        settings: RetrievalSettings | None = None,
    ) -> RetrievalResult:
        engine = create_engine(self.env.harness.settings, application_name="plummy-test-retrieval")
        try:
            use_case = RetrieveChunks(
                SqlAlchemyUnitOfWorkFactory(create_session_factory(engine)),
                FakeGateway(self.embedder),  # type: ignore[arg-type]
                self.env.harness.resources.secrets,
            )
            return await use_case.execute(
                who, [UUID(kb) for kb in kb_ids], query, mode=mode, settings=settings
            )
        finally:
            await engine.dispose()


def files(result: RetrievalResult) -> list[str]:
    return [item.hit.filename for item in result.chunks]


async def chunk_text(env: Env, document_id: str, contains: str) -> str:
    [(content,)] = await env.harness.sql(
        "SELECT content FROM document_chunks WHERE document_id = :id AND content LIKE :pattern",
        id=document_id,
        pattern=f"%{contains}%",
    )
    return str(content)


@pytest.fixture
async def corpus(env: Env) -> dict[str, Any]:
    embedder = TextEmbedder()
    leave = await indexed(env, env.kb_id, "leave.md", LEAVE, embedder)
    expenses = await indexed(env, env.kb_id, "expenses.md", EXPENSES, embedder)
    admin_id = (await env.admin.get("/api/v1/auth/me")).json()["user"]["id"]
    return {"embedder": embedder, "leave": leave, "expenses": expenses, "admin": context(admin_id)}


# --- branches ---------------------------------------------------------------------------------------------


async def test_keyword_branch_matches_russian_and_english_word_forms(
    env: Env, corpus: dict[str, Any]
) -> None:
    search = Retriever(env, corpus["embedder"])
    manager = context(corpus["admin"].user_id, "knowledge_bases.manage")
    # "отпуск сотрудника" stems to the same lexemes as "Сотрудники ... отпуска"; "reimburse" as "reimbursed".
    ru = await search(manager, [env.kb_id], "отпуск сотрудника", mode=SearchMode.KEYWORD)
    assert files(ru)[0] == "leave.md"
    assert ru.chunks[0].keyword_rank == 1
    en = await search(manager, [env.kb_id], "reimburse travel", mode=SearchMode.KEYWORD)
    assert files(en) == ["expenses.md"]
    assert all(item.vector_rank is None for item in en.chunks)


async def test_keyword_query_of_only_stop_words_falls_back_to_vectors(
    env: Env, corpus: dict[str, Any]
) -> None:
    manager = context(corpus["admin"].user_id, "knowledge_bases.manage")
    result = await Retriever(env, corpus["embedder"])(manager, [env.kb_id], "и в на the of")
    assert result.keyword_query_empty
    assert result.keyword_candidates == 0
    assert result.vector_candidates > 0
    assert result.chunks  # the vector branch still answers


async def test_vector_branch_ranks_the_nearest_chunk_first(env: Env, corpus: dict[str, Any]) -> None:
    manager = context(corpus["admin"].user_id, "knowledge_bases.manage")
    target = await chunk_text(env, corpus["expenses"], "Hotel costs")
    result = await Retriever(env, corpus["embedder"])(manager, [env.kb_id], target, mode=SearchMode.VECTOR)
    best = result.chunks[0]
    assert best.hit.content == target
    assert best.hit.filename == "expenses.md"
    assert best.vector_rank == 1
    assert best.hit.score == pytest.approx(1.0, abs=1e-5)  # cosine distance 0


async def test_hybrid_fuses_both_branches_with_citation_metadata(env: Env, corpus: dict[str, Any]) -> None:
    manager = context(corpus["admin"].user_id, "knowledge_bases.manage")
    target = await chunk_text(env, corpus["leave"], "перенести")
    result = await Retriever(env, corpus["embedder"])(manager, [env.kb_id], target)
    best = result.chunks[0]
    assert best.vector_rank == 1
    assert best.keyword_rank is not None
    assert best.hit.filename == "leave.md"
    assert best.hit.section_path
    assert best.hit.document_id == UUID(corpus["leave"])
    assert result.total_tokens == sum(item.hit.token_count for item in result.chunks)


async def test_index_scan_and_exact_scan_agree(
    env: Env, corpus: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    manager = context(corpus["admin"].user_id, "knowledge_bases.manage")
    engine = create_engine(env.harness.settings)
    try:
        profile_id = UUID(
            (await env.admin.get(f"/api/v1/knowledge-bases/{env.kb_id}")).json()["embedding_profile_id"]
        )
        await EnsureVectorIndex(
            SqlAlchemyUnitOfWorkFactory(create_session_factory(engine)), PgVectorIndexManager(engine)
        ).execute(profile_id)
    finally:
        await engine.dispose()
    target = await chunk_text(env, corpus["leave"], "28")
    search = Retriever(env, corpus["embedder"])
    exact = await search(manager, [env.kb_id], target, mode=SearchMode.VECTOR)
    monkeypatch.setattr(sql_retrieval, "EXACT_SCAN_THRESHOLD", 0)  # force the HNSW path
    indexed_scan = await search(manager, [env.kb_id], target, mode=SearchMode.VECTOR)
    assert [c.hit.chunk_id for c in exact.chunks][:1] == [c.hit.chunk_id for c in indexed_scan.chunks][:1]


# --- access and data filters (§5.11, SC-05) ---------------------------------------------------------------


async def test_user_without_access_gets_not_found_and_no_provider_call(
    env: Env, corpus: dict[str, Any]
) -> None:
    member = await active_user(env.harness, env.admin, "member@example.com", [])
    calls = len(corpus["embedder"].calls)
    with pytest.raises(NotFoundError):
        await Retriever(env, corpus["embedder"])(context(member), [env.kb_id], "отпуск")
    assert len(corpus["embedder"].calls) == calls  # authorized before any embedding


async def test_read_grant_searches_only_the_granted_knowledge_base(env: Env, corpus: dict[str, Any]) -> None:
    other_kb = await create_kb(env, "Finance")
    await indexed(env, other_kb, "salaries.md", SECRET, corpus["embedder"])
    member = await active_user(env.harness, env.admin, "reader@example.com", [])
    await grant(env, env.kb_id, member)
    search = Retriever(env, corpus["embedder"])

    [(salaries,)] = await env.harness.sql("SELECT id FROM documents WHERE filename = 'salaries.md'")
    target = await chunk_text(env, str(salaries), "salary")
    result = await search(context(member), [env.kb_id], target)  # the exact text of the other KB's chunk
    assert "salaries.md" not in files(result)
    keyword = await search(context(member), [env.kb_id], "salary review March", mode=SearchMode.KEYWORD)
    assert keyword.chunks == ()
    with pytest.raises(NotFoundError):
        await search(context(member), [env.kb_id, other_kb], "salary")  # one KB without access: all refused


async def test_deleted_documents_are_not_found(env: Env, corpus: dict[str, Any]) -> None:
    manager = context(corpus["admin"].user_id, "knowledge_bases.manage")
    target = await chunk_text(env, corpus["leave"], "перенести")
    assert (await env.admin.delete(f"/api/v1/documents/{corpus['leave']}")).status_code == 204
    search = Retriever(env, corpus["embedder"])
    for mode in SearchMode:
        result = await search(manager, [env.kb_id], target, mode=mode)
        assert "leave.md" not in files(result), mode


@pytest.mark.parametrize(
    "breakage",
    [
        "UPDATE document_versions SET status = 'failed' WHERE document_id = :id",
        "UPDATE document_versions SET status = 'outdated' WHERE document_id = :id",
        "UPDATE documents SET current_version_id = NULL WHERE id = :id",
        "UPDATE document_versions SET active_chunk_set = active_chunk_set + 1 WHERE document_id = :id",
    ],
)
async def test_only_the_active_chunk_set_of_the_current_ready_version_is_searched(
    env: Env, corpus: dict[str, Any], breakage: str
) -> None:
    manager = context(corpus["admin"].user_id, "knowledge_bases.manage")
    target = await chunk_text(env, corpus["leave"], "перенести")
    await env.harness.sql(breakage, id=corpus["leave"])
    search = Retriever(env, corpus["embedder"])
    for mode in SearchMode:
        assert "leave.md" not in files(await search(manager, [env.kb_id], target, mode=mode)), mode
    assert "expenses.md" in files(await search(manager, [env.kb_id], "hotel", mode=SearchMode.KEYWORD))


async def test_vector_branch_uses_only_the_profile_of_the_knowledge_base(
    env: Env, corpus: dict[str, Any]
) -> None:
    # A second KB with another profile includes the same source: its chunks have no vectors of that profile.
    provider_id = (await env.admin.get("/api/v1/providers")).json()["items"][0]["id"]
    profile = await env.admin.post(
        "/api/v1/embedding-profiles",
        json={"name": "E2", "provider_id": provider_id, "model_id": "m2", "dimensions": 8},
    )
    other_kb = await create_kb(env, "Mirror", profile.json()["id"])
    await env.harness.sql(
        "INSERT INTO knowledge_base_sources (knowledge_base_id, data_source_id, created_at) "
        "SELECT :kb, data_source_id, now() FROM documents WHERE id = :doc",
        kb=other_kb,
        doc=corpus["leave"],
    )
    manager = context(corpus["admin"].user_id, "knowledge_bases.manage")
    search = Retriever(env, corpus["embedder"])
    target = await chunk_text(env, corpus["leave"], "перенести")
    assert (await search(manager, [other_kb], target, mode=SearchMode.VECTOR)).chunks == ()
    assert files(await search(manager, [other_kb], "отпуск", mode=SearchMode.KEYWORD))  # FTS is profile-free
    with pytest.raises(InvalidInputError) as raised:
        await search(manager, [env.kb_id, other_kb], "отпуск")
    assert raised.value.error_code == "mixed_embedding_profiles"


# --- budget and errors ------------------------------------------------------------------------------------


async def test_context_budget_and_top_k_bound_the_result(env: Env, corpus: dict[str, Any]) -> None:
    manager = context(corpus["admin"].user_id, "knowledge_bases.manage")
    search = Retriever(env, corpus["embedder"])
    everything = await search(manager, [env.kb_id], "отпуск expenses hotel")
    assert len(everything.chunks) >= 2
    one = await search(manager, [env.kb_id], "отпуск expenses hotel", settings=RetrievalSettings(top_k=1))
    assert len(one.chunks) == 1
    smallest = min(item.hit.token_count for item in everything.chunks)
    tight = await search(
        manager,
        [env.kb_id],
        "отпуск expenses hotel",
        settings=RetrievalSettings(max_context_tokens=max(256, smallest)),
    )
    assert tight.total_tokens <= max(256, smallest)


async def test_provider_failures_are_typed_and_carry_no_output(env: Env, corpus: dict[str, Any]) -> None:
    manager = context(corpus["admin"].user_id, "knowledge_bases.manage")
    embedder: FakeEmbedder = corpus["embedder"]
    search = Retriever(env, embedder)
    embedder.failures[len(embedder.calls)] = ProviderAuthError()
    with pytest.raises(UpstreamProviderError) as failed:
        await search(manager, [env.kb_id], "отпуск")
    assert failed.value.code == "provider_error"
    assert failed.value.reason == "provider_auth_failed"
    embedder.failures[len(embedder.calls)] = ProviderTimeoutError()
    with pytest.raises(UpstreamTimeoutError):
        await search(manager, [env.kb_id], "отпуск")
    # Keyword-only search needs no provider at all.
    embedder.failures[len(embedder.calls)] = ProviderAuthError()
    assert (await search(manager, [env.kb_id], "отпуск", mode=SearchMode.KEYWORD)).chunks


async def test_disabled_provider_makes_embedding_unavailable(env: Env, corpus: dict[str, Any]) -> None:
    manager = context(corpus["admin"].user_id, "knowledge_bases.manage")
    await env.harness.sql("UPDATE ai_providers SET enabled = false")
    with pytest.raises(EmbeddingUnavailableError):
        await Retriever(env, corpus["embedder"])(manager, [env.kb_id], "отпуск")


async def test_invalid_queries(env: Env, corpus: dict[str, Any]) -> None:
    manager = context(corpus["admin"].user_id, "knowledge_bases.manage")
    search = Retriever(env, corpus["embedder"])
    for query, code in (("  ", "empty_query"), ("x" * 1001, "query_too_long")):
        with pytest.raises(InvalidInputError) as raised:
            await search(manager, [env.kb_id], query)
        assert raised.value.error_code == code
    with pytest.raises(InvalidInputError):
        await search(manager, [], "отпуск")
    with pytest.raises(NotFoundError):
        await search(manager, [str(uuid4())], "отпуск")
