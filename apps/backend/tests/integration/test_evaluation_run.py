"""``RunRetrievalEvaluation`` end to end: real upload, ingestion and indexing use cases, PostgreSQL and
access checks. The workers are replaced by the ``sleep`` hook, which runs the queued jobs the way the
Celery tasks do; vectors come from the deterministic fake embedder."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from plummy.application.ai.ports import ProviderBadRequestError
from plummy.application.errors import NotFoundError, PermissionDeniedError
from plummy.application.evaluation.dataset import EvalDataset, parse_dataset
from plummy.application.evaluation.runner import (
    EvalOptions,
    EvaluationError,
    RunRetrievalEvaluation,
    operator_context,
)
from plummy.application.retrieval.model import SearchMode
from plummy.application.retrieval.use_cases import RetrieveChunks
from plummy.infrastructure.clock import SystemClock
from plummy.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWorkFactory
from tests.integration.harness import running_auth_app
from tests.integration.rbac import active_user
from tests.integration.test_document_upload import Env, make_env, run
from tests.integration.test_indexing import FakeGateway, index
from tests.integration.test_retrieval import TextEmbedder

pytestmark = pytest.mark.integration

FILES = {
    "leave.md": "\n\n".join(["# Отпуск", "Сотрудники получают 28 дней отпуска.", "## Перенос",
                             "Перенести можно пять дней."]).encode(),
    "vpn.txt": b"VPN sessions end after 12 hours; personal devices are not allowed.",
    "phones.csv": b"team;phone\nhelpdesk;+7 000 01\nsecurity;+7 000 11\n",
}  # fmt: skip
MANIFEST = {
    "name": "tiny",
    "documents": sorted(FILES),
    "questions": [
        {"id": "q1", "question": "отпуск сотрудники дней", "language": "ru",
         "relevant": [{"document": "leave.md", "evidence": "28 дней"}]},
        {"id": "q2", "question": "VPN sessions hours", "language": "en", "tags": ["it"],
         "relevant": [{"document": "vpn.txt", "evidence": "12 hours"}]},
        {"id": "q3", "question": "security phone", "language": "en", "tags": ["table"],
         "relevant": [{"document": "phones.csv", "evidence": "+7 000 11"}]},
        {"id": "q4", "question": "Who is the CEO?", "language": "en", "tags": ["unanswerable"]},
    ],
}  # fmt: skip


@pytest.fixture
async def env(
    migrated_database_url: str, redis_url: str, clean_rate_limits: None, tmp_path: Path
) -> AsyncIterator[Env]:
    async with running_auth_app(migrated_database_url, redis_url, tmp_path / "bootstrap") as harness:
        yield await make_env(harness)


def dataset(files: dict[str, bytes] = FILES) -> EvalDataset:
    return parse_dataset(json.dumps(MANIFEST).encode(), files)


class Workers:
    """Stands in for ``worker-ingestion`` and ``worker``: each wait runs every queued job to the end."""

    def __init__(self, env: Env, embedder: TextEmbedder) -> None:
        self.env, self.embedder, self.waits = env, embedder, 0

    async def __call__(self, _seconds: float) -> None:
        self.waits += 1
        rows = await self.env.harness.sql("SELECT id FROM ingestion_jobs WHERE status = 'queued' ORDER BY id")
        for (job_id,) in rows:
            await run(self.env, str(job_id))
            await index(self.env, str(job_id), self.embedder)


def evaluation(env: Env, embedder: TextEmbedder, workers: Workers) -> RunRetrievalEvaluation:
    resources = env.harness.resources
    uow = SqlAlchemyUnitOfWorkFactory(resources.session_factory)
    retrieve = RetrieveChunks(uow, FakeGateway(embedder), resources.secrets)  # type: ignore[arg-type]
    return RunRetrievalEvaluation(
        uow, resources.knowledge.create, resources.documents.upload, retrieve, SystemClock(), sleep=workers
    )


async def profile_id(env: Env) -> UUID:
    kb = (await env.admin.get(f"/api/v1/knowledge-bases/{env.kb_id}")).json()
    return UUID(kb["embedding_profile_id"])


async def admin_context(env: Env) -> Any:
    uow = SqlAlchemyUnitOfWorkFactory(env.harness.resources.session_factory)
    me = (await env.admin.get("/api/v1/auth/me")).json()["user"]
    return await operator_context(uow, SystemClock(), me["email"])


async def test_a_run_indexes_the_dataset_and_reports_metrics_without_text(env: Env) -> None:
    embedder = TextEmbedder()
    workers = Workers(env, embedder)
    progress: list[str] = []
    options = EvalOptions(await profile_id(env), ks=(1, 3))
    report = await evaluation(env, embedder, workers).execute(
        await admin_context(env), dataset(), options, progress.append
    )

    assert report["dataset"]["documents"] == 3
    assert report["dataset"]["answerable"] == 3
    assert report["dataset"]["unanswerable"] == 1
    assert set(report["runs"]) == {"hybrid", "vector", "keyword"}
    keyword = report["runs"]["keyword"]["summary"]
    assert keyword["all"]["questions"] == 3
    assert keyword["all"]["recall"]["3"] == 1.0
    assert keyword["all"]["mrr"] == 1.0
    assert keyword["table"]["questions"] == 1
    assert "unanswerable" not in keyword
    q4 = next(q for q in report["runs"]["hybrid"]["questions"] if q["id"] == "q4")
    assert q4["answerable"] is False
    assert q4["recall"] == {}
    text = json.dumps(report, ensure_ascii=False)
    assert "Сотрудники получают" not in text  # no document text
    assert "Who is the CEO" not in text  # no question text
    assert "indexing: done" in progress
    [(count,)] = await env.harness.sql(
        "SELECT count(*) FROM documents d "
        "JOIN knowledge_base_sources s ON s.data_source_id = d.data_source_id "
        "WHERE s.knowledge_base_id = :kb AND d.processing_status = 'indexed'",
        kb=report["knowledge_base_id"],
    )
    assert count == 3


async def test_a_second_run_reuses_the_knowledge_base_and_uploads_nothing(env: Env) -> None:
    embedder = TextEmbedder()
    workers = Workers(env, embedder)
    options = EvalOptions(await profile_id(env), modes=(SearchMode.KEYWORD,), ks=(1,))
    context = await admin_context(env)
    first = await evaluation(env, embedder, workers).execute(context, dataset(), options)
    [(documents_before,)] = await env.harness.sql("SELECT count(*) FROM documents")
    second = await evaluation(env, embedder, workers).execute(context, dataset(), options)
    [(documents_after,)] = await env.harness.sql("SELECT count(*) FROM documents")
    assert second["knowledge_base_id"] == first["knowledge_base_id"]
    assert documents_after == documents_before
    assert second["runs"]["keyword"]["summary"] == first["runs"]["keyword"]["summary"]


async def test_a_changed_dataset_gets_its_own_knowledge_base(env: Env) -> None:
    embedder = TextEmbedder()
    workers = Workers(env, embedder)
    options = EvalOptions(await profile_id(env), modes=(SearchMode.KEYWORD,), ks=(1,))
    context = await admin_context(env)
    first = await evaluation(env, embedder, workers).execute(context, dataset(), options)
    changed = dict(FILES, **{"vpn.txt": FILES["vpn.txt"] + b" Updated."})
    second = await evaluation(env, embedder, workers).execute(context, dataset(changed), options)
    assert second["knowledge_base_id"] != first["knowledge_base_id"]


async def test_a_document_that_fails_to_index_stops_the_run(env: Env) -> None:
    embedder = TextEmbedder()
    for call in range(10):
        embedder.failures[call] = ProviderBadRequestError()  # permanent: the documents end up failed
    with pytest.raises(EvaluationError, match="failed to index"):
        await evaluation(env, embedder, Workers(env, embedder)).execute(
            await admin_context(env), dataset(), EvalOptions(await profile_id(env), ks=(1,))
        )


async def test_indexing_that_never_finishes_times_out(env: Env) -> None:
    embedder = TextEmbedder()

    async def idle(_seconds: float) -> None:  # no workers at all
        return None

    resources = env.harness.resources
    uow = SqlAlchemyUnitOfWorkFactory(resources.session_factory)
    runner = RunRetrievalEvaluation(
        uow,
        resources.knowledge.create,
        resources.documents.upload,
        RetrieveChunks(uow, FakeGateway(embedder), resources.secrets),  # type: ignore[arg-type]
        SystemClock(),
        sleep=idle,
    )
    options = EvalOptions(await profile_id(env), ks=(1,), indexing_timeout_seconds=0)
    with pytest.raises(EvaluationError, match="did not finish"):
        await runner.execute(await admin_context(env), dataset(), options)


async def test_only_a_knowledge_base_manager_can_run_it(env: Env) -> None:
    embedder = TextEmbedder()
    await active_user(env.harness, env.admin, "member@example.com", [])
    uow = SqlAlchemyUnitOfWorkFactory(env.harness.resources.session_factory)
    member = await operator_context(uow, SystemClock(), "member@example.com")
    assert "knowledge_bases.manage" not in member.permissions
    with pytest.raises(PermissionDeniedError):
        await evaluation(env, embedder, Workers(env, embedder)).execute(
            member, dataset(), EvalOptions(await profile_id(env), ks=(1,))
        )
    [(count,)] = await env.harness.sql("SELECT count(*) FROM knowledge_bases WHERE name LIKE 'Evaluation %'")
    assert count == 0


async def test_operator_context_needs_an_active_user(env: Env) -> None:
    uow = SqlAlchemyUnitOfWorkFactory(env.harness.resources.session_factory)
    with pytest.raises(NotFoundError):
        await operator_context(uow, SystemClock(), "nobody@example.com")
    user_id = await active_user(env.harness, env.admin, "gone@example.com", [])
    await env.harness.sql("UPDATE users SET status = 'disabled' WHERE id = :id", id=user_id)
    with pytest.raises(NotFoundError):
        await operator_context(uow, SystemClock(), "gone@example.com")
    admin = await admin_context(env)
    assert "knowledge_bases.manage" in admin.permissions
