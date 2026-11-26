"""Documents and jobs API, deletion with purge and the maintenance sweeps; real PostgreSQL, Redis, storage.

API_CONTRACTS.md §2.11, §2.15; ADR-008 (lifecycle, idempotency); DATABASE_SCHEMA.md §8 (deleted documents
leave retrieval at once and their derived data is purged); SC-05 for documents (no access → ``404``).
"""

from __future__ import annotations

import hashlib
import os
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import aclosing, asynccontextmanager
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest

from plummy.application.ai.ports import CancellationToken, ProviderAuthError
from plummy.application.chat.runs import SweepChat
from plummy.application.ingestion.maintenance import (
    CleanupQuarantine,
    PurgeDocument,
    RequeueJobs,
    SweepDeletedDocuments,
)
from plummy.application.ingestion.pipeline import RunOutcome
from plummy.application.sources.sync import ScheduleSyncs
from plummy.application.storage import StorageArea, StorageKey
from plummy.composition import MaintenanceServices
from plummy.infrastructure.clock import SystemClock
from plummy.infrastructure.db.engine import create_engine, create_session_factory
from plummy.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWorkFactory
from plummy.workers.app import (
    EMBEDDINGS_QUEUE,
    INDEXING_TASK,
    INGESTION_QUEUE,
    INGESTION_TASK,
    MAINTENANCE_QUEUE,
    PURGE_TASK,
)
from tests.integration.harness import running_auth_app
from tests.integration.rbac import audit_actions
from tests.integration.test_document_upload import (
    Env,
    files_in,
    job_row,
    make_env,
    problem,
    run,
    signed_in,
    upload,
    uploaded,
)
from tests.integration.test_indexing import MARKDOWN, FakeEmbedder, counts, extracted, index

pytestmark = pytest.mark.integration

DOCS = "/api/v1/documents"
JOBS = "/api/v1/jobs"
MISSING = "00000000-0000-7000-8000-000000000000"


@pytest.fixture
async def env(
    migrated_database_url: str, redis_url: str, clean_rate_limits: None, tmp_path: Path
) -> AsyncIterator[Env]:
    async with running_auth_app(migrated_database_url, redis_url, tmp_path / "bootstrap") as harness:
        yield await make_env(harness)


@asynccontextmanager
async def maintenance(env: Env) -> AsyncIterator[MaintenanceServices]:
    """What the maintenance tasks build, against the harness database, storage and queue namespace."""
    engine = create_engine(env.harness.settings, application_name="plummy-test-worker")
    uow = SqlAlchemyUnitOfWorkFactory(create_session_factory(engine))
    clock, jobs = SystemClock(), env.harness.resources.jobs
    try:
        yield MaintenanceServices(
            purge=PurgeDocument(uow, env.storage, clock),
            sweep_deleted=SweepDeletedDocuments(uow, jobs, clock),
            requeue=RequeueJobs(uow, jobs, clock),
            cleanup_quarantine=CleanupQuarantine(env.storage, clock, ttl=env.harness.settings.quarantine_ttl),
            sweep_chat=SweepChat(uow, clock, timeout_seconds=120),
            schedule_syncs=ScheduleSyncs(uow, jobs, clock),
        )
    finally:
        await engine.dispose()


async def indexed(env: Env, filename: str = "policy.md", data: bytes = MARKDOWN) -> tuple[str, str]:
    job_id, document_id = await extracted(env, filename, data)
    assert await index(env, job_id, FakeEmbedder()) is RunOutcome.SUCCEEDED
    return job_id, document_id


async def failed_at_embedding(env: Env) -> tuple[str, str]:
    """A document whose index phase failed permanently (the provider rejected the key)."""
    job_id, document_id = await extracted(env)
    embedder = FakeEmbedder(failures={0: ProviderAuthError()})
    assert await index(env, job_id, embedder) is RunOutcome.FAILED
    return job_id, document_id


async def grant(env: Env, user_id: str, level: str) -> None:
    await env.harness.sql(
        "INSERT INTO knowledge_base_access (id, knowledge_base_id, principal_type, user_id, access_level) "
        "VALUES (gen_random_uuid(), :kb, 'user', :user, :level)",
        kb=env.kb_id,
        user=user_id,
        level=level,
    )


def tasks_in(env: Env, queue: str, task: str) -> list[list[str]]:
    return [
        eval(message["headers"]["argsrepr"])  # noqa: S307 - repr of a list of UUID strings written by kombu
        for message in env.harness.queued_messages(queue)
        if message["headers"]["task"] == task
    ]


def stored_files(env: Env) -> int:
    return len(files_in(env.storage, StorageArea.QUARANTINE)) + len(
        files_in(env.storage, StorageArea.DOCUMENTS)
    )


# --- list, card, versions -----------------------------------------------------------------------------------


async def test_list_card_and_versions(env: Env) -> None:
    _, indexed_id = await indexed(env)
    _, pending_id = await uploaded(env, "Notes.txt", b"pending text")

    listing = await env.admin.get(DOCS, params={"knowledge_base_id": env.kb_id})
    assert listing.status_code == 200, listing.text
    body = listing.json()
    assert [item["id"] for item in body["items"]] == [pending_id, indexed_id]  # newest first
    assert (body["has_more"], body["next_cursor"]) == (False, None)
    assert listing.headers["cache-control"] == "no-store"

    card = (await env.admin.get(f"{DOCS}/{indexed_id}")).json()
    assert card["filename"] == "policy.md"
    assert card["data_source"]["type"] == "upload"
    assert card["external_id"] == f"upload:{UUID(indexed_id).hex}"
    assert card["processing_status"] == "indexed"
    current = card["current_version"]
    assert (current["version_number"], current["status"], current["error_code"]) == (1, "ready", None)
    assert current["checksum_sha256"] == hashlib.sha256(MARKDOWN).hexdigest()
    assert current["size_bytes"] == len(MARKDOWN)
    assert card["latest_version"] == current

    pending = (await env.admin.get(f"{DOCS}/{pending_id}")).json()
    assert pending["current_version"] is None
    assert pending["latest_version"]["status"] == "pending"

    versions = (await env.admin.get(f"{DOCS}/{indexed_id}/versions")).json()["items"]
    assert [v["id"] for v in versions] == [current["id"]]

    by_status = await env.admin.get(
        DOCS, params={"knowledge_base_id": env.kb_id, "processing_status": "indexed"}
    )
    assert [item["id"] for item in by_status.json()["items"]] == [indexed_id]
    search = await env.admin.get(DOCS, params={"knowledge_base_id": env.kb_id, "q": "NOTES"})
    assert [item["id"] for item in search.json()["items"]] == [pending_id]
    wildcard = await env.admin.get(DOCS, params={"knowledge_base_id": env.kb_id, "q": "%"})
    assert wildcard.json()["items"] == []  # LIKE wildcards are matched literally


async def test_list_pagination_and_strict_query(env: Env) -> None:
    ids = [(await uploaded(env, f"doc-{n}.txt", b"text"))[1] for n in range(3)]
    seen: list[str] = []
    cursor: str | None = None
    while True:
        params = {"knowledge_base_id": env.kb_id, "limit": "2", "sort": "filename"}
        if cursor:
            params["cursor"] = cursor
        page = (await env.admin.get(DOCS, params=params)).json()
        seen += [item["id"] for item in page["items"]]
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == ids

    problem(await env.admin.get(DOCS), 422, "validation_error")  # knowledge_base_id is required
    problem(
        await env.admin.get(DOCS, params={"knowledge_base_id": env.kb_id, "tenant": "x"}),
        422,
        "invalid_query_parameter",
    )
    problem(
        await env.admin.get(DOCS, params={"knowledge_base_id": env.kb_id, "sort": "storage_key"}),
        422,
        "invalid_query_parameter",
    )
    other_source = await env.admin.get(
        DOCS, params={"knowledge_base_id": env.kb_id, "data_source_id": MISSING}
    )
    assert other_source.json()["items"] == []
    problem(await env.admin.get(DOCS, params={"knowledge_base_id": MISSING}), 404, "not_found")


async def test_documents_are_hidden_without_access_sc05(env: Env) -> None:
    _, document_id = await indexed(env)
    member, member_id = await signed_in(env, "reader@example.com", ["member"])
    async with aclosing(member):
        missing = problem(await member.get(f"{DOCS}/{MISSING}"), 404, "not_found")
        hidden = problem(await member.get(f"{DOCS}/{document_id}"), 404, "not_found")
        assert hidden["detail"] == missing["detail"]  # existence is not disclosed
        problem(await member.get(f"{DOCS}/{document_id}/versions"), 404, "not_found")
        problem(await member.get(DOCS, params={"knowledge_base_id": env.kb_id}), 404, "not_found")
        problem(await member.delete(f"{DOCS}/{document_id}"), 404, "not_found")
        problem(await member.post(f"{DOCS}/{document_id}/reindex"), 404, "not_found")

        await grant(env, member_id, "read")
        assert (await member.get(f"{DOCS}/{document_id}")).status_code == 200
        listing = await member.get(DOCS, params={"knowledge_base_id": env.kb_id})
        assert [item["id"] for item in listing.json()["items"]] == [document_id]
        problem(await member.delete(f"{DOCS}/{document_id}"), 403, "permission_denied")
        problem(await member.post(f"{DOCS}/{document_id}/reindex"), 403, "permission_denied")

        # a document reached through a deleted knowledge base is gone for everyone
        await env.harness.sql("UPDATE knowledge_bases SET deleted_at = now()")
        problem(await member.get(f"{DOCS}/{document_id}"), 404, "not_found")
    problem(await env.admin.get(f"{DOCS}/{document_id}"), 404, "not_found")


# --- deletion and purge -------------------------------------------------------------------------------------


async def test_delete_hides_at_once_and_purge_removes_everything(env: Env) -> None:
    _, document_id = await indexed(env)
    _, other_id = await indexed(env, "other.md", b"# Other\n\nKept text.")
    assert await counts(env, document_id) == (3, 3)
    files_before = stored_files(env)

    response = await env.admin.delete(f"{DOCS}/{document_id}")
    assert response.status_code == 204, response.text
    problem(await env.admin.get(f"{DOCS}/{document_id}"), 404, "not_found")
    listing = await env.admin.get(DOCS, params={"knowledge_base_id": env.kb_id})
    assert [item["id"] for item in listing.json()["items"]] == [other_id]
    kb = (await env.admin.get(f"/api/v1/knowledge-bases/{env.kb_id}")).json()
    assert kb["indexing"]["documents_total"] == 1
    assert "document.deleted" in await audit_actions(env.harness)
    assert tasks_in(env, MAINTENANCE_QUEUE, PURGE_TASK) == [[document_id]]
    problem(await env.admin.delete(f"{DOCS}/{document_id}"), 404, "not_found")  # already deleted

    async with maintenance(env) as services:
        assert await services.purge.execute(UUID(document_id)) is True
        assert await services.purge.execute(UUID(document_id)) is False  # idempotent
        assert await services.purge.execute(UUID(other_id)) is False  # not deleted: untouched
    assert await counts(env, document_id) == (0, 0)
    assert await counts(env, other_id) == (1, 1)
    assert stored_files(env) == files_before - 2  # the original and the extracted text
    [(purged, reason, statuses)] = await env.harness.sql(
        "SELECT d.purged_at IS NOT NULL, d.deletion_reason, array_agg(v.status) FROM documents d "
        "JOIN document_versions v ON v.document_id = d.id WHERE d.id = :id GROUP BY d.id",
        id=document_id,
    )
    assert (purged, reason, statuses) == (True, "user", ["deleted"])


async def test_delete_cancels_queued_jobs_and_the_worker_stops(env: Env) -> None:
    job_id, document_id = await uploaded(env, "policy.md", MARKDOWN)
    assert (await env.admin.delete(f"{DOCS}/{document_id}")).status_code == 204
    assert await job_row(env, job_id) == ("cancelled", "fetch", None, 0, "document_deleted")
    assert await run(env, job_id) is RunOutcome.SKIPPED  # a late message finds a finished job
    async with maintenance(env) as services:
        assert await services.purge.execute(UUID(document_id)) is True
    assert stored_files(env) == 0


class DeletingEmbedder(FakeEmbedder):
    """Deletes the document through the API while the provider call is in flight."""

    def __init__(self, client: httpx.AsyncClient, document_id: str) -> None:
        super().__init__(max_batch=1)
        self.client, self.document_id = client, document_id

    async def embed(
        self, texts: Sequence[str], *, model_id: str, cancel: CancellationToken
    ) -> list[list[float]]:
        if not self.calls:
            response = await self.client.delete(f"{DOCS}/{self.document_id}")
            assert response.status_code == 204, response.text
        return await super().embed(texts, model_id=model_id, cancel=cancel)


async def test_delete_during_indexing_writes_nothing_after_it(env: Env) -> None:
    job_id, document_id = await extracted(env)
    embedder = DeletingEmbedder(env.admin, document_id)
    assert await index(env, job_id, embedder) is RunOutcome.CANCELLED
    assert len(embedder.calls) == 1  # stopped at the first write after the deletion
    assert await counts(env, document_id) == (3, 0)  # chunks written before the deletion, no vectors
    assert (await job_row(env, job_id))[0] == "cancelled"
    [(current,)] = await env.harness.sql(
        "SELECT current_version_id FROM documents WHERE id = :id", id=document_id
    )
    assert current is None  # never activated
    async with maintenance(env) as services:
        assert await services.purge.execute(UUID(document_id)) is True
    assert await counts(env, document_id) == (0, 0)
    assert stored_files(env) == 0


async def test_delete_after_the_file_moved_removes_the_stored_copy(env: Env) -> None:
    """The extract phase stops at the first transaction after the deletion and cleans up what it wrote."""
    job_id, document_id = await uploaded(env, "policy.md", MARKDOWN)

    class DeletingStorage:
        def __init__(self) -> None:
            self.inner, self.deleted = env.storage, False

        async def move(self, key: StorageKey, source: StorageArea, target: StorageArea) -> None:
            await self.inner.move(key, source, target)
            response = await env.admin.delete(f"{DOCS}/{document_id}")
            assert response.status_code == 204, response.text
            async with maintenance(env) as services:  # the purge runs before the worker continues
                assert await services.purge.execute(UUID(document_id)) is True

        def __getattr__(self, name: str) -> Any:
            return getattr(self.inner, name)

    assert await run(env, job_id, storage=DeletingStorage()) is RunOutcome.CANCELLED
    assert stored_files(env) == 0
    assert (await job_row(env, job_id))[0] == "cancelled"


# --- reindex -----------------------------------------------------------------------------------------------


async def test_reindex_a_failed_document_from_the_stored_text(env: Env) -> None:
    failed_job, document_id = await failed_at_embedding(env)
    assert (await env.admin.get(f"{DOCS}/{document_id}")).json()["latest_version"]["error_code"] == (
        "provider_auth_failed"
    )
    response = await env.admin.post(f"{DOCS}/{document_id}/reindex")
    assert response.status_code == 202, response.text
    body = response.json()
    job = body["job"]
    assert body["created"] is True
    assert (job["kind"], job["status"], job["stage"], job["attempt"]) == ("reindex", "queued", "chunk", 0)
    assert job["id"] != failed_job
    assert [job["id"]] in tasks_in(env, EMBEDDINGS_QUEUE, INDEXING_TASK)  # no parsing again
    card = (await env.admin.get(f"{DOCS}/{document_id}")).json()
    assert (card["processing_status"], card["latest_version"]["status"]) == ("pending", "pending")
    assert "document.reindex.requested" in await audit_actions(env.harness)

    # a second request while the job waits returns the same job
    again = (await env.admin.post(f"{DOCS}/{document_id}/reindex")).json()
    assert (again["job"]["id"], again["created"]) == (job["id"], False)

    assert await index(env, job["id"], FakeEmbedder()) is RunOutcome.SUCCEEDED
    assert (await env.admin.get(f"{DOCS}/{document_id}")).json()["processing_status"] == "indexed"
    assert await counts(env, document_id) == (3, 3)


async def test_reindex_of_an_indexed_version_changes_nothing(env: Env) -> None:
    _, document_id = await indexed(env)
    job = (await env.admin.post(f"{DOCS}/{document_id}/reindex")).json()["job"]
    assert (await env.admin.get(f"{DOCS}/{document_id}")).json()["processing_status"] == "indexed"
    embedder = FakeEmbedder()
    assert await index(env, job["id"], embedder) is RunOutcome.SUCCEEDED
    assert await job_row(env, job["id"]) == ("succeeded", "chunk", "skipped_already_processed", 0, None)
    assert (await counts(env, document_id), embedder.calls) == ((3, 3), [])


async def test_reindex_before_extraction_starts_from_the_original(env: Env) -> None:
    upload_job, document_id = await uploaded(env, "notes.txt", b"plain text")
    await env.harness.sql(
        "UPDATE ingestion_jobs SET status = 'dead', error_code = 'attempts_exhausted' WHERE id = :id",
        id=upload_job,
    )
    job = (await env.admin.post(f"{DOCS}/{document_id}/reindex")).json()["job"]
    assert job["stage"] == "fetch"
    assert [job["id"]] in tasks_in(env, INGESTION_QUEUE, INGESTION_TASK)
    assert await run(env, job["id"]) is RunOutcome.HANDED_OFF


async def test_reindex_and_retry_share_the_expensive_limit(env: Env) -> None:
    _, document_id = await indexed(env)
    for _ in range(10):
        response = await env.admin.post(f"{DOCS}/{document_id}/reindex")
        assert response.status_code == 202, response.text
        await env.harness.sql(
            "UPDATE ingestion_jobs SET status = 'failed', error_code = 'x' WHERE status = 'queued'"
        )
    problem(await env.admin.post(f"{DOCS}/{document_id}/reindex"), 429, "rate_limited")
    [(failed_job,)] = await env.harness.sql("SELECT id FROM ingestion_jobs WHERE status = 'failed' LIMIT 1")
    problem(await env.admin.post(f"{JOBS}/ingestion/{failed_job}/retry"), 429, "rate_limited")


# --- jobs API ----------------------------------------------------------------------------------------------


async def test_jobs_list_and_details(env: Env) -> None:
    done_job, _ = await indexed(env)
    queued_job, _ = await uploaded(env, "b.txt", b"text")

    listing = await env.admin.get(JOBS)
    assert listing.status_code == 200, listing.text
    items = listing.json()["items"]
    assert [item["id"] for item in items] == [queued_job, done_job]
    done = next(item for item in items if item["id"] == done_job)
    assert (done["type"], done["kind"], done["status"], done["result"]) == (
        "ingestion",
        "ingest",
        "succeeded",
        "processed",
    )
    assert done["error"] is None

    queued = await env.admin.get(JOBS, params={"status": "queued"})
    assert [item["id"] for item in queued.json()["items"]] == [queued_job]
    by_kb = await env.admin.get(JOBS, params={"knowledge_base_id": env.kb_id})
    assert len(by_kb.json()["items"]) == 2
    assert (await env.admin.get(JOBS, params={"knowledge_base_id": MISSING})).json()["items"] == []
    assert (await env.admin.get(JOBS, params={"data_source_id": MISSING})).json()["items"] == []
    assert (await env.admin.get(JOBS, params={"type": "sync"})).json()["items"] == []
    problem(await env.admin.get(JOBS, params={"tenant": "x"}), 422, "invalid_query_parameter")

    detail = await env.admin.get(f"{JOBS}/ingestion/{done_job}")
    assert detail.json()["id"] == done_job
    problem(await env.admin.get(f"{JOBS}/ingestion/{MISSING}"), 404, "not_found")
    problem(await env.admin.get(f"{JOBS}/sync/{done_job}"), 404, "not_found")
    assert (await env.admin.get(f"{JOBS}/other/{done_job}")).status_code == 422


async def test_job_errors_are_shown_redacted(env: Env) -> None:
    job_id, _ = await failed_at_embedding(env)
    job = (await env.admin.get(f"{JOBS}/ingestion/{job_id}")).json()
    assert job["status"] == "failed"
    assert job["error"]["code"] == "provider_auth_failed"
    assert "Policy" not in str(job) and "Leave" not in str(job)  # noqa: PT018 - no document text


async def test_jobs_need_permissions(env: Env) -> None:
    job_id, _ = await failed_at_embedding(env)
    member, _ = await signed_in(env, "member@example.com", ["member"])
    async with aclosing(member):
        problem(await member.get(JOBS), 403, "permission_denied")
        problem(await member.get(f"{JOBS}/ingestion/{job_id}"), 403, "permission_denied")
        problem(await member.post(f"{JOBS}/ingestion/{job_id}/retry"), 403, "permission_denied")
    manager, _ = await signed_in(env, "km@example.com", ["knowledge_manager"])
    async with aclosing(manager):
        assert (await manager.get(JOBS)).status_code == 200
        assert (await manager.post(f"{JOBS}/ingestion/{job_id}/retry")).status_code == 202


async def test_retry_a_failed_job(env: Env) -> None:
    job_id, document_id = await failed_at_embedding(env)
    response = await env.admin.post(f"{JOBS}/ingestion/{job_id}/retry")
    assert response.status_code == 202, response.text
    job = response.json()
    assert (job["id"], job["status"], job["stage"], job["attempt"], job["error"]) == (
        job_id,
        "queued",
        "chunk",
        0,
        None,
    )
    assert (job["started_at"], job["finished_at"]) == (None, None)
    assert [job_id] in tasks_in(env, EMBEDDINGS_QUEUE, INDEXING_TASK)
    assert "job.retried" in await audit_actions(env.harness)
    assert (await env.admin.get(f"{DOCS}/{document_id}")).json()["processing_status"] == "pending"

    problem(await env.admin.post(f"{JOBS}/ingestion/{job_id}/retry"), 409, "job_not_retryable")  # queued now
    assert await index(env, job_id, FakeEmbedder()) is RunOutcome.SUCCEEDED
    assert await job_row(env, job_id) == ("succeeded", "finalize", "processed", 1, None)
    problem(await env.admin.post(f"{JOBS}/ingestion/{job_id}/retry"), 409, "job_not_retryable")  # succeeded
    problem(await env.admin.post(f"{JOBS}/ingestion/{MISSING}/retry"), 404, "not_found")


async def test_retry_is_refused_for_a_deleted_document(env: Env) -> None:
    job_id, document_id = await failed_at_embedding(env)
    assert (await env.admin.delete(f"{DOCS}/{document_id}")).status_code == 204
    problem(await env.admin.post(f"{JOBS}/ingestion/{job_id}/retry"), 409, "job_not_retryable")
    assert (await job_row(env, job_id))[0] == "failed"


# --- cancel -------------------------------------------------------------------------------------------------


async def document_state(env: Env, document_id: str) -> tuple[str, str | None, str | None]:
    """Document status, the latest version's status and error code."""
    [row] = await env.harness.sql(
        "SELECT d.processing_status, v.status, v.error_code FROM documents d "
        "JOIN document_versions v ON v.document_id = d.id WHERE d.id = :id "
        "ORDER BY v.version_number DESC LIMIT 1",
        id=document_id,
    )
    return row[0], row[1], row[2]


async def test_cancel_a_queued_job_then_retry_it(env: Env) -> None:
    job_id, document_id = await uploaded(env, "policy.md", MARKDOWN)
    response = await env.admin.post(f"{JOBS}/ingestion/{job_id}/cancel")
    assert response.status_code == 200, response.text
    job = response.json()
    assert (job["status"], job["error"]["code"]) == ("cancelled", "cancelled_by_admin")
    assert job["finished_at"] is not None
    assert await document_state(env, document_id) == ("failed", "failed", "cancelled_by_admin")
    assert "job.cancelled" in await audit_actions(env.harness)
    [(details,)] = await env.harness.sql(
        "SELECT details FROM audit_logs WHERE action = 'job.cancelled' AND target_id = :id", id=job_id
    )
    assert details == {"document_id": document_id, "previous_status": "queued"}
    assert await run(env, job_id) is RunOutcome.SKIPPED  # the queued message finds a finished job
    problem(await env.admin.post(f"{JOBS}/ingestion/{job_id}/cancel"), 409, "job_not_cancellable")

    retried = await env.admin.post(f"{JOBS}/ingestion/{job_id}/retry")
    assert retried.status_code == 202, retried.text
    assert (retried.json()["status"], retried.json()["error"]) == ("queued", None)
    assert await document_state(env, document_id) == ("pending", "pending", None)
    assert await run(env, job_id) is RunOutcome.HANDED_OFF
    assert await index(env, job_id, FakeEmbedder()) is RunOutcome.SUCCEEDED
    assert (await env.admin.get(f"{DOCS}/{document_id}")).json()["processing_status"] == "indexed"
    problem(await env.admin.post(f"{JOBS}/ingestion/{job_id}/cancel"), 409, "job_not_cancellable")  # done


class CancellingEmbedder(FakeEmbedder):
    """Cancels the job through the API while the provider call is in flight."""

    def __init__(self, client: httpx.AsyncClient, job_id: str) -> None:
        super().__init__(max_batch=1)
        self.client, self.job_id = client, job_id

    async def embed(
        self, texts: Sequence[str], *, model_id: str, cancel: CancellationToken
    ) -> list[list[float]]:
        if not self.calls:
            response = await self.client.post(f"{JOBS}/ingestion/{self.job_id}/cancel")
            assert response.status_code == 200, response.text
            assert response.json()["status"] == "cancelled"
        return await super().embed(texts, model_id=model_id, cancel=cancel)


async def test_cancel_during_indexing_stops_the_worker_and_activates_nothing(env: Env) -> None:
    job_id, document_id = await extracted(env)
    embedder = CancellingEmbedder(env.admin, job_id)
    assert await index(env, job_id, embedder) is RunOutcome.CANCELLED
    assert len(embedder.calls) == 1  # stopped at the first write after the cancellation
    assert await counts(env, document_id) == (3, 0)  # an inactive chunk set without vectors
    [(current,)] = await env.harness.sql(
        "SELECT current_version_id FROM documents WHERE id = :id", id=document_id
    )
    assert current is None
    found = await env.admin.post(
        f"/api/v1/knowledge-bases/{env.kb_id}/search", json={"query": "policy", "mode": "keyword"}
    )
    assert found.status_code == 200, found.text
    assert found.json()["items"] == []
    # A retry resumes from the stored text and reuses the chunks without duplicating them.
    assert (await env.admin.post(f"{JOBS}/ingestion/{job_id}/retry")).status_code == 202
    assert await index(env, job_id, FakeEmbedder()) is RunOutcome.SUCCEEDED
    assert await counts(env, document_id) == (3, 3)


async def test_cancel_a_reindex_keeps_the_current_version(env: Env) -> None:
    _, document_id = await indexed(env)
    reindex = await env.admin.post(f"/api/v1/knowledge-bases/{env.kb_id}/reindex")
    [job_id] = reindex.json()["job_ids"]
    assert (await env.admin.post(f"{JOBS}/ingestion/{job_id}/cancel")).status_code == 200
    assert await document_state(env, document_id) == ("indexed", "ready", None)
    found = await env.admin.post(
        f"/api/v1/knowledge-bases/{env.kb_id}/search", json={"query": "policy", "mode": "keyword"}
    )
    assert {hit["document_id"] for hit in found.json()["items"]} == {document_id}


async def test_cancel_needs_jobs_manage(env: Env) -> None:
    job_id, _ = await uploaded(env, "policy.md", MARKDOWN)
    member, _ = await signed_in(env, "member@example.com", ["member"])
    async with aclosing(member):
        problem(await member.post(f"{JOBS}/ingestion/{job_id}/cancel"), 403, "permission_denied")
    assert (await job_row(env, job_id))[0] == "queued"
    problem(await env.admin.post(f"{JOBS}/ingestion/{MISSING}/cancel"), 404, "not_found")
    problem(await env.admin.post(f"{JOBS}/sync/{job_id}/cancel"), 404, "not_found")
    problem(await env.admin.post(f"{JOBS}/other/{job_id}/cancel"), 422, "validation_error")
    manager, _ = await signed_in(env, "km@example.com", ["knowledge_manager"])
    async with aclosing(manager):
        assert (await manager.post(f"{JOBS}/ingestion/{job_id}/cancel")).status_code == 200


async def test_jobs_cancelled_by_a_deletion_are_not_retried(env: Env) -> None:
    job_id, document_id = await uploaded(env, "policy.md", MARKDOWN)
    assert (await env.admin.delete(f"{DOCS}/{document_id}")).status_code == 204
    problem(await env.admin.post(f"{JOBS}/ingestion/{job_id}/retry"), 409, "job_not_retryable")
    problem(await env.admin.post(f"{JOBS}/ingestion/{job_id}/cancel"), 409, "job_not_cancellable")


# --- maintenance sweeps ------------------------------------------------------------------------------------


async def test_requeue_publishes_jobs_the_broker_never_accepted(env: Env) -> None:
    stale_job, _ = await uploaded(env, "a.txt", b"text")
    recent_job, _ = await uploaded(env, "b.txt", b"text")
    sent_job, _ = await uploaded(env, "c.txt", b"text")
    await env.harness.sql(
        "UPDATE ingestion_jobs SET celery_task_id = NULL, updated_at = now() - interval '10 minutes' "
        "WHERE id = :id",
        id=stale_job,
    )
    await env.harness.sql("UPDATE ingestion_jobs SET celery_task_id = NULL WHERE id = :id", id=recent_job)
    await env.harness.sql(
        "UPDATE ingestion_jobs SET updated_at = now() - interval '10 minutes' WHERE id = :id", id=sent_job
    )
    before = tasks_in(env, INGESTION_QUEUE, INGESTION_TASK)
    async with maintenance(env) as services:
        assert await services.requeue.execute() == 1
        assert await services.requeue.execute() == 0  # published now
    after = tasks_in(env, INGESTION_QUEUE, INGESTION_TASK)
    assert len(after) == len(before) + 1
    assert after.count([stale_job]) == 2  # the upload and the sweep
    [(task_id,)] = await env.harness.sql(
        "SELECT celery_task_id FROM ingestion_jobs WHERE id = :id", id=stale_job
    )
    assert task_id is not None


async def test_sweep_purges_deletions_whose_message_was_lost(env: Env) -> None:
    _, old_id = await uploaded(env, "a.txt", b"text")
    _, recent_id = await uploaded(env, "b.txt", b"text")
    await env.harness.sql(
        "UPDATE documents SET deleted_at = now() - interval '1 hour', deletion_reason = 'user' "
        "WHERE id = :id",
        id=old_id,
    )
    await env.harness.sql(
        "UPDATE documents SET deleted_at = now(), deletion_reason = 'user' WHERE id = :id", id=recent_id
    )
    async with maintenance(env) as services:
        assert await services.sweep_deleted.execute() == 1
        assert await services.purge.execute(UUID(old_id)) is True
        assert await services.sweep_deleted.execute() == 0
    assert tasks_in(env, MAINTENANCE_QUEUE, PURGE_TASK) == [[old_id]]


async def test_quarantine_cleanup_removes_only_expired_files(env: Env) -> None:
    await uploaded(env, "old.txt", b"old")
    [old_file] = files_in(env.storage, StorageArea.QUARANTINE)
    expired = time.time() - env.harness.settings.quarantine_ttl.total_seconds() - 60
    os.utime(old_file, (expired, expired))
    await uploaded(env, "new.txt", b"new")
    async with maintenance(env) as services:
        assert await services.cleanup_quarantine.execute() == 1
    assert not old_file.exists()
    assert len(files_in(env.storage, StorageArea.QUARANTINE)) == 1


async def test_source_filter_only_narrows_the_listing(env: Env) -> None:
    """Another knowledge base's source does not widen a listing (the filter only narrows)."""
    kb = (await env.admin.get(f"/api/v1/knowledge-bases/{env.kb_id}")).json()
    other = await env.admin.post(
        "/api/v1/knowledge-bases", json={"name": "Other", "embedding_profile_id": kb["embedding_profile_id"]}
    )
    other_kb = other.json()["id"]
    assert (await upload(env.admin, other_kb, "x.txt", b"text")).status_code == 202
    [(other_source,)] = await env.harness.sql(
        "SELECT data_source_id FROM knowledge_base_sources WHERE knowledge_base_id = :kb", kb=other_kb
    )
    listing = await env.admin.get(
        DOCS, params={"knowledge_base_id": env.kb_id, "data_source_id": str(other_source)}
    )
    assert listing.json()["items"] == []
