"""Index phase (stages 8-12) on real PostgreSQL + pgvector, Redis and storage; ADR-008, ADR-017, SC-09.

The embedding provider is a deterministic stand-in behind ``ProviderGateway`` (the HTTP adapters have their
own contract tests); the key is still decrypted by the real vault and the vectors go through the real SQL.
"""

from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from plummy.application.ai.ports import (
    CancellationToken,
    EmbeddingCapabilities,
    EmbeddingProvider,
    ProviderAuthError,
    ProviderConnection,
    ProviderError,
    ProviderTimeoutError,
)
from plummy.application.ingestion.indexing import RunIndexingJob
from plummy.application.ingestion.pipeline import RunOutcome
from plummy.application.knowledge.vector_index import EnsureVectorIndex, index_name, index_spec
from plummy.domain.ai import EmbeddingProfile
from plummy.infrastructure.chunking.structured import StructuredChunker
from plummy.infrastructure.clock import SystemClock
from plummy.infrastructure.db.engine import create_engine, create_session_factory
from plummy.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWorkFactory
from plummy.infrastructure.db.vector_index import PgVectorIndexManager
from plummy.workers.app import MAINTENANCE_QUEUE, VECTOR_INDEX_TASK
from tests.integration.harness import running_auth_app
from tests.integration.test_document_upload import FAKE_KEY, Env, docx_bytes, job_row, make_env, run, uploaded

pytestmark = pytest.mark.integration

DIMENSIONS = 8  # the profile of make_env
MARKDOWN = "\n\n".join(
    ["# Policy", "Intro text.", "## Leave", "Leave is 28 days.", "## Transfers", "Ask your manager."]
).encode()


@dataclass
class FakeEmbedder:
    """Deterministic vectors from the text hash. ``failures`` are raised before the matching call."""

    max_batch: int = 64
    dimensions: int = DIMENSIONS
    failures: dict[int, ProviderError] = field(default_factory=dict)
    calls: list[list[str]] = field(default_factory=list)
    connections: list[ProviderConnection] = field(default_factory=list)

    def embedding_capabilities(self) -> EmbeddingCapabilities:
        return EmbeddingCapabilities(max_batch=self.max_batch)

    async def test_connection(self, *, timeout: float) -> Any:  # noqa: ASYNC109
        raise NotImplementedError

    async def embed(
        self, texts: Sequence[str], *, model_id: str, cancel: CancellationToken
    ) -> list[list[float]]:
        number = len(self.calls)
        self.calls.append(list(texts))
        if number in self.failures:
            raise self.failures[number]
        return [
            [b / 255 + 0.01 for b in hashlib.sha256(t.encode()).digest()[: self.dimensions]] for t in texts
        ]

    @property
    def embedded(self) -> list[str]:
        return [text for call in self.calls for text in call]


class FakeGateway:
    def __init__(self, embedder: FakeEmbedder) -> None:
        self.embedder = embedder

    def embedding(self, connection: ProviderConnection) -> EmbeddingProvider:
        self.embedder.connections.append(connection)
        return self.embedder

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(f"unexpected gateway call {name}")


@pytest.fixture
async def env(
    migrated_database_url: str, redis_url: str, clean_rate_limits: None, tmp_path: Path
) -> AsyncIterator[Env]:
    async with running_auth_app(migrated_database_url, redis_url, tmp_path / "bootstrap") as harness:
        yield await make_env(harness)


async def index(env: Env, job_id: str, embedder: FakeEmbedder) -> RunOutcome:
    """What the ``index_job`` task does, against the harness database and storage."""
    engine = create_engine(env.harness.settings, application_name="plummy-test-worker")
    try:
        runner = RunIndexingJob(
            SqlAlchemyUnitOfWorkFactory(create_session_factory(engine)),
            env.storage,
            SystemClock(),
            chunker=StructuredChunker(),
            gateway=FakeGateway(embedder),  # type: ignore[arg-type]
            vault=env.harness.resources.secrets,
            indexes=PgVectorIndexManager(engine),
            jobs=env.harness.resources.jobs,
        )
        return await runner.execute(UUID(job_id))
    finally:
        await engine.dispose()


async def extracted(env: Env, filename: str = "policy.md", data: bytes = MARKDOWN) -> tuple[str, str]:
    job_id, document_id = await uploaded(env, filename, data)
    assert await run(env, job_id) is RunOutcome.HANDED_OFF
    return job_id, document_id


async def counts(env: Env, document_id: str) -> tuple[int, int]:
    [(chunks, vectors)] = await env.harness.sql(
        "SELECT (SELECT count(*) FROM document_chunks WHERE document_id = :id), "
        "(SELECT count(*) FROM embeddings e JOIN document_chunks c ON c.id = e.chunk_id "
        "WHERE c.document_id = :id)",
        id=document_id,
    )
    return int(chunks), int(vectors)


async def state(env: Env, document_id: str) -> tuple[Any, ...]:
    [row] = await env.harness.sql(
        "SELECT d.processing_status, v.status, v.active_chunk_set, d.current_version_id = v.id, v.error_code "
        "FROM documents d JOIN document_versions v ON v.document_id = d.id WHERE d.id = :id",
        id=document_id,
    )
    return tuple(row)


async def test_document_is_chunked_embedded_and_switched_atomically(env: Env) -> None:
    job_id, document_id = await extracted(env)
    embedder = FakeEmbedder()
    assert await index(env, job_id, embedder) is RunOutcome.SUCCEEDED
    # one attempt: the hand-off between the phases is not counted
    assert await job_row(env, job_id) == ("succeeded", "finalize", "processed", 1, None)
    assert await state(env, document_id) == ("indexed", "ready", 1, True, None)

    rows = await env.harness.sql(
        "SELECT chunk_index, content, section_path, token_count FROM document_chunks "
        "WHERE document_id = :id ORDER BY chunk_index",
        id=document_id,
    )
    assert [(r.chunk_index, r.content, r.section_path) for r in rows] == [
        (0, "Policy\n\nIntro text.", ["Policy"]),
        (1, "Leave\n\nLeave is 28 days.", ["Policy", "Leave"]),
        (2, "Transfers\n\nAsk your manager.", ["Policy", "Transfers"]),
    ]
    assert embedder.embedded == [r.content for r in rows]  # chunk order, each once
    assert await counts(env, document_id) == (3, 3)
    [(dims, profile_ok)] = await env.harness.sql(
        "SELECT DISTINCT e.dimensions, e.embedding_config_id = kb.embedding_config_id FROM embeddings e "
        "JOIN knowledge_bases kb ON kb.id = :kb",
        kb=env.kb_id,
    )
    assert (dims, profile_ok) == (DIMENSIONS, True)
    # the provider key was decrypted by the vault for this call only
    [connection] = embedder.connections
    assert connection.api_key is not None
    assert connection.api_key.reveal() == FAKE_KEY

    kb = (await env.admin.get(f"/api/v1/knowledge-bases/{env.kb_id}")).json()
    assert kb["indexing"] == {"documents_total": 1, "indexed": 1, "processing": 0, "failed": 0}


async def test_vectors_are_normalized_when_the_profile_says_so(env: Env) -> None:
    await env.harness.sql("UPDATE embedding_provider_configs SET normalize = true")
    job_id, document_id = await extracted(env)
    assert await index(env, job_id, FakeEmbedder()) is RunOutcome.SUCCEEDED
    norms = await env.harness.sql(
        "SELECT round(vector_norm(embedding)::numeric, 6) FROM embeddings WHERE document_version_id = "
        "(SELECT current_version_id FROM documents WHERE id = :id)",
        id=document_id,
    )
    assert {float(n) for (n,) in norms} == {1.0}


async def test_repeated_job_changes_nothing_sc09(env: Env) -> None:
    job_id, document_id = await extracted(env)
    embedder = FakeEmbedder()
    assert await index(env, job_id, embedder) is RunOutcome.SUCCEEDED
    before, calls = await counts(env, document_id), len(embedder.calls)

    # a retry of the finished job (the jobs API, or a lost acknowledgement) for the indexed version
    await env.harness.sql(
        "UPDATE ingestion_jobs SET status = 'queued', result = NULL, finished_at = NULL WHERE id = :id",
        id=job_id,
    )
    assert await index(env, job_id, embedder) is RunOutcome.SUCCEEDED
    assert await job_row(env, job_id) == ("succeeded", "finalize", "skipped_already_processed", 1, None)
    await env.harness.sql(
        "UPDATE ingestion_jobs SET status = 'queued', result = NULL WHERE id = :id", id=job_id
    )
    assert await run(env, job_id) is RunOutcome.SUCCEEDED  # the extract phase skips it too
    assert (await counts(env, document_id), len(embedder.calls)) == (before, calls)
    assert await index(env, job_id, embedder) is RunOutcome.SKIPPED  # finished: a redelivery is ignored


async def test_concurrent_runs_do_not_duplicate_rows(env: Env) -> None:
    job_id, document_id = await extracted(env)
    outcomes = await asyncio.gather(*(index(env, job_id, FakeEmbedder(max_batch=1)) for _ in range(3)))
    assert RunOutcome.SUCCEEDED in outcomes
    assert set(outcomes) <= {RunOutcome.SUCCEEDED, RunOutcome.SKIPPED, RunOutcome.RETRY}
    assert await counts(env, document_id) == (3, 3)
    assert await state(env, document_id) == ("indexed", "ready", 1, True, None)


async def test_transient_provider_error_resumes_where_it_stopped(env: Env) -> None:
    job_id, document_id = await extracted(env)
    embedder = FakeEmbedder(max_batch=2, failures={1: ProviderTimeoutError()})
    assert await index(env, job_id, embedder) is RunOutcome.RETRY
    assert await job_row(env, job_id) == ("queued", "embed", None, 1, "transient_error")
    assert await counts(env, document_id) == (3, 2)  # the first batch is kept
    assert (await state(env, document_id))[1:3] == ("processing", None)  # not visible to retrieval

    assert await index(env, job_id, embedder) is RunOutcome.SUCCEEDED
    assert await job_row(env, job_id) == ("succeeded", "finalize", "processed", 2, None)
    assert await counts(env, document_id) == (3, 3)
    assert len(embedder.embedded) == 4  # 2 + the failed batch of 1 + its retry; nothing embedded twice
    assert embedder.calls[1] == embedder.calls[2]


async def test_transient_errors_end_dead(env: Env) -> None:
    job_id, document_id = await extracted(env)
    embedder = FakeEmbedder(failures={n: ProviderTimeoutError() for n in range(10)})
    outcomes = [await index(env, job_id, embedder) for _ in range(5)]
    assert outcomes == [RunOutcome.RETRY] * 4 + [RunOutcome.DEAD]
    assert await job_row(env, job_id) == ("dead", "embed", None, 5, "attempts_exhausted")
    assert (await state(env, document_id))[0] == "failed"


@pytest.mark.parametrize(
    ("embedder", "code"),
    [
        (FakeEmbedder(failures={0: ProviderAuthError()}), "provider_auth_failed"),
        (FakeEmbedder(dimensions=DIMENSIONS + 1), "embedding_dimensions_mismatch"),
    ],
)
async def test_permanent_errors_fail_without_activation(env: Env, embedder: FakeEmbedder, code: str) -> None:
    job_id, document_id = await extracted(env)
    assert await index(env, job_id, embedder) is RunOutcome.FAILED
    assert await job_row(env, job_id) == ("failed", "embed", None, 1, code)
    assert await state(env, document_id) == ("failed", "failed", None, None, code)
    assert (await counts(env, document_id))[1] == 0


async def test_disabled_provider_fails_the_job(env: Env) -> None:
    job_id, _ = await extracted(env)
    await env.harness.sql("UPDATE ai_providers SET enabled = false")
    embedder = FakeEmbedder()
    assert await index(env, job_id, embedder) is RunOutcome.FAILED
    assert (await job_row(env, job_id))[4] == "provider_unavailable"
    assert embedder.calls == []


async def test_state_is_rechecked_before_indexing(env: Env) -> None:
    job_id, document_id = await extracted(env)
    await env.harness.sql(
        "UPDATE documents SET deleted_at = now(), deletion_reason = 'user' WHERE id = :id", id=document_id
    )
    embedder = FakeEmbedder()
    assert await index(env, job_id, embedder) is RunOutcome.CANCELLED
    assert (await job_row(env, job_id))[4] == "resource_unavailable"
    assert embedder.calls == []
    assert await counts(env, document_id) == (0, 0)


async def test_index_message_before_extraction_is_ignored(env: Env) -> None:
    job_id, _ = await uploaded(env, "notes.docx", docx_bytes())
    assert await index(env, job_id, FakeEmbedder()) is RunOutcome.SKIPPED
    assert await job_row(env, job_id) == ("queued", "fetch", None, 0, None)


async def test_new_version_replaces_the_current_one(env: Env) -> None:
    job_id, document_id = await extracted(env)
    assert await index(env, job_id, FakeEmbedder()) is RunOutcome.SUCCEEDED
    [(old_version,)] = await env.harness.sql(
        "SELECT current_version_id FROM documents WHERE id = :id", id=document_id
    )
    # a second version of the same document (connector sync, Phase 9), run through both phases
    other_job, other_document = await extracted(env, "other.md", b"# Other\n\nNew text.")
    [(new_version,)] = await env.harness.sql(
        "SELECT id FROM document_versions WHERE document_id = :id", id=other_document
    )
    await env.harness.sql(
        "UPDATE document_versions SET document_id = :doc, version_number = 2 WHERE id = :v",
        doc=document_id,
        v=new_version,
    )
    await env.harness.sql(
        "UPDATE ingestion_jobs SET document_id = :doc WHERE id = :job", doc=document_id, job=other_job
    )
    assert await index(env, other_job, FakeEmbedder()) is RunOutcome.SUCCEEDED
    versions = await env.harness.sql(
        "SELECT v.id = d.current_version_id, v.status FROM document_versions v JOIN documents d "
        "ON d.id = v.document_id WHERE v.document_id = :id ORDER BY v.version_number",
        id=document_id,
    )
    assert [tuple(row) for row in versions] == [(False, "outdated"), (True, "ready")]
    assert old_version != new_version


async def profile_of(engine: AsyncEngine, profile_id: UUID) -> EmbeddingProfile:
    async with SqlAlchemyUnitOfWorkFactory(create_session_factory(engine))() as uow:
        profile = await uow.embedding_profiles.get(profile_id)
    assert profile is not None
    return profile


async def test_kb_creation_requests_the_vector_index_and_the_task_builds_it(env: Env) -> None:
    [message] = env.harness.queued_messages(MAINTENANCE_QUEUE)
    assert message["headers"]["task"] == VECTOR_INDEX_TASK
    [(profile_id,)] = await env.harness.sql("SELECT embedding_config_id FROM knowledge_bases")
    assert message["headers"]["argsrepr"] == repr([str(profile_id)])

    engine = create_engine(env.harness.settings, application_name="plummy-test-worker")
    try:
        use_case = EnsureVectorIndex(
            SqlAlchemyUnitOfWorkFactory(create_session_factory(engine)), PgVectorIndexManager(engine)
        )
        assert await use_case.execute(profile_id) is True
        assert await use_case.execute(profile_id) is False  # idempotent
        [(definition,)] = await env.harness.sql(
            "SELECT indexdef FROM pg_indexes WHERE indexname = :name", name=index_name(profile_id)
        )
        assert "USING hnsw (((embedding)::vector(8)) vector_cosine_ops)" in definition
        assert f"embedding_config_id = '{profile_id}'::uuid" in definition
        [(valid,)] = await env.harness.sql(
            "SELECT indisvalid FROM pg_index WHERE indexrelid = CAST(:name AS regclass)",
            name=index_name(profile_id),
        )
        assert valid is True

        # An interrupted CREATE INDEX CONCURRENTLY leaves an invalid index under the name; a failed concurrent
        # build (a unique index over duplicate values) produces the same state. It is dropped and rebuilt.
        job_id, _ = await extracted(env)
        assert await index(env, job_id, FakeEmbedder()) is RunOutcome.SUCCEEDED  # 3 vectors, same dimensions
        name = index_name(profile_id)
        assert await PgVectorIndexManager(engine).drop(profile_id) is True
        owner = create_async_engine(env.harness.owner_url)  # DDL of the test itself: the schema owner
        try:
            async with owner.connect() as connection:
                autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
                with pytest.raises(IntegrityError):
                    await autocommit.execute(
                        text(f"CREATE UNIQUE INDEX CONCURRENTLY {name} ON embeddings (dimensions)")
                    )
        finally:
            await owner.dispose()
        assert await PgVectorIndexManager(engine).is_ready(await profile_of(engine, profile_id)) is False
        assert await use_case.execute(profile_id) is True
        [(valid, definition)] = await env.harness.sql(
            "SELECT i.indisvalid, pg_get_indexdef(i.indexrelid) FROM pg_index i "
            "WHERE i.indexrelid = CAST(:name AS regclass)",
            name=name,
        )
        assert valid is True
        assert "hnsw" in definition

        await env.harness.sql("UPDATE embedding_provider_configs SET status = 'retired'")
        await PgVectorIndexManager(engine).drop(profile_id)
        assert await use_case.execute(profile_id) is False
        assert (
            await env.harness.sql("SELECT 1 FROM pg_indexes WHERE indexname = :n", n=index_name(profile_id))
            == []
        )
        assert await use_case.execute(UUID(int=1)) is False  # unknown profile
    finally:
        await engine.dispose()


@pytest.mark.parametrize(
    ("dimensions", "metric"),
    [(8, "cosine"), (8, "l2"), (2000, "inner_product"), (2001, "cosine"), (4000, "l2")],
)
async def test_the_owner_function_builds_exactly_the_index_retrieval_expects(
    env: Env, dimensions: int, metric: str
) -> None:
    """Revision 0017 builds the index in SQL; retrieval uses ``index_spec`` (ADR-017 §3): they must agree."""
    [(profile_id,)] = await env.harness.sql("SELECT embedding_config_id FROM knowledge_bases")
    await env.harness.sql(
        "UPDATE embedding_provider_configs SET dimensions = :d, distance_metric = :m WHERE id = :id",
        d=dimensions,
        m=metric,
        id=profile_id,
    )
    engine = create_engine(env.harness.settings, application_name="plummy-test-worker")  # the runtime role
    try:
        profile = await profile_of(engine, profile_id)
        manager = PgVectorIndexManager(engine)
        assert await manager.ensure(profile) is True
        assert await manager.ensure(profile) is False
        assert await manager.is_ready(profile) is True
    finally:
        await engine.dispose()
    spec = index_spec(profile)
    reference = spec.name[:-8] + "ref00000"
    await env.harness.sql(
        spec.create_sql().replace("CONCURRENTLY ", "").replace(spec.name, reference)
    )  # the reference DDL, as the owner
    [(built,)] = await env.harness.sql("SELECT pg_get_indexdef(CAST(:n AS regclass))", n=spec.name)
    [(expected,)] = await env.harness.sql("SELECT pg_get_indexdef(CAST(:n AS regclass))", n=reference)
    assert built == expected.replace(reference, spec.name)


async def test_the_index_functions_refuse_what_no_profile_allows(env: Env) -> None:
    [(profile_id,)] = await env.harness.sql("SELECT embedding_config_id FROM knowledge_bases")
    await env.harness.sql(
        "UPDATE embedding_provider_configs SET dimensions = 4001 WHERE id = :id", id=profile_id
    )
    engine = create_engine(env.harness.settings, application_name="plummy-test-worker")
    try:
        async with engine.begin() as connection:
            with pytest.raises(Exception, match="not indexable"):
                await connection.scalar(
                    text("SELECT public.plummy_ensure_vector_index(:id)"), {"id": profile_id}
                )
        async with engine.begin() as connection:  # an unknown profile: nothing to build or drop
            assert (
                await connection.scalar(
                    text("SELECT public.plummy_ensure_vector_index(:id)"), {"id": UUID(int=7)}
                )
                is False
            )
            assert (
                await connection.scalar(
                    text("SELECT public.plummy_drop_vector_index(:id)"), {"id": UUID(int=7)}
                )
                is False
            )
    finally:
        await engine.dispose()
    assert await env.harness.sql("SELECT 1 FROM pg_indexes WHERE indexname LIKE 'ix_embeddings_hnsw_%'") == []
