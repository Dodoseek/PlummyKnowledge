"""Sync of a Google Drive source (ADR-020 §8) against a fake Drive on loopback, real PostgreSQL and Redis.

The runner is the one the ``connectors`` task builds: the real ``GoogleOAuthClient`` and
``GoogleDriveConnector`` through the app's ``SafeHttpClient`` and network policy, the real vault and storage.
Downloaded files go into quarantine and become ingestion jobs; a part of the tests runs them through the
pipeline to prove that a file deleted in Drive leaves search.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest

from plummy.application.ingestion.indexing import RunIndexingJob
from plummy.application.ingestion.parsing import ParserRegistry
from plummy.application.ingestion.pipeline import RunIngestionJob, RunOutcome
from plummy.application.retrieval.model import RetrievalScope
from plummy.application.sources.sync import RunSyncJob, ScheduleSyncs, SyncRunOutcome, SyncRunResult
from plummy.composition import oauth_clients, source_connectors
from plummy.infrastructure.chunking.structured import StructuredChunker
from plummy.infrastructure.clock import SystemClock
from plummy.infrastructure.db.engine import create_engine, create_session_factory
from plummy.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWorkFactory
from plummy.infrastructure.db.vector_index import PgVectorIndexManager
from plummy.infrastructure.parsing.text import PlainTextParser
from plummy.workers.app import CONNECTORS_QUEUE, INGESTION_QUEUE, MAINTENANCE_QUEUE, PURGE_TASK, SYNC_TASK
from tests.fake_google import CLIENT_ID, CLIENT_SECRET, FakeGoogle, connector_config
from tests.fake_http import FakeHttpServer, Reply, serve
from tests.integration.harness import Harness, running_auth_app
from tests.integration.rbac import active_user, login, setup_admin_client
from tests.integration.test_document_upload import StubOfficeParser, docx_bytes
from tests.integration.test_indexing import FakeEmbedder, FakeGateway

pytestmark = pytest.mark.integration

URL = "/api/v1/data-sources"
DOC = "application/vnd.google-apps.document"
ALPHA = b"Alpha policy: leave is 28 days.\n"
BETA = b"# Beta\n\nBeta contract terms.\n"


@dataclass
class Env:
    harness: Harness
    admin: httpx.AsyncClient
    google: FakeGoogle
    server: FakeHttpServer
    kb_id: str


@dataclass
class Tree:
    folder: str
    sub: str
    alpha: str
    beta: str
    plan: str
    outside: str


@pytest.fixture
async def env(
    migrated_database_url: str, redis_url: str, clean_rate_limits: None, tmp_path: Path
) -> AsyncIterator[Env]:
    google = FakeGoogle()
    async with serve(google.handle) as server:
        config = tmp_path / "plummy.toml"
        config.write_text(connector_config(server.port))
        async with running_auth_app(
            migrated_database_url,
            redis_url,
            tmp_path / "bootstrap",
            config_file=config,
            google_oauth_client_id=CLIENT_ID,
            google_oauth_client_secret=CLIENT_SECRET,
        ) as harness:
            admin = await setup_admin_client(harness)
            provider = await admin.post(
                "/api/v1/providers",
                json={
                    "name": "OpenAI",
                    "provider_type": "openai",
                    "base_url": "https://1.1.1.1/v1",
                    "api_key": "k",
                },
            )
            assert provider.status_code == 201, provider.text
            profile = await admin.post(
                "/api/v1/embedding-profiles",
                json={
                    "name": "Small",
                    "provider_id": provider.json()["id"],
                    "model_id": "m",
                    "dimensions": 8,
                },
            )
            assert profile.status_code == 201, profile.text
            kb = await admin.post(
                "/api/v1/knowledge-bases",
                json={"name": "Sales", "embedding_profile_id": profile.json()["id"]},
            )
            assert kb.status_code == 201, kb.text
            yield Env(harness, admin, google, server, str(kb.json()["id"]))


def build_tree(google: FakeGoogle) -> Tree:
    folder = google.folder("Sales")
    sub = google.folder("Contracts", parent=folder)
    alpha = google.add("alpha.txt", parent=folder, content=ALPHA)
    beta = google.add("beta.md", parent=sub, mime="text/markdown", content=BETA)
    plan = google.add("Plan", parent=folder, mime=DOC, content=docx_bytes())
    google.add("Link", parent=folder, mime="application/vnd.google-apps.shortcut")
    google.add("Survey", parent=folder, mime="application/vnd.google-apps.form")
    outside = google.add("outside.txt", content=b"Not in the folder.\n")
    return Tree(folder, sub, alpha, beta, plan, outside)


def set_content(google: FakeGoogle, item_id: str, data: bytes) -> None:
    resource = google.files[item_id]
    google.contents[item_id] = data
    if "md5Checksum" in resource:
        resource["md5Checksum"] = hashlib.md5(data).hexdigest()  # noqa: S324 - as Drive does
        resource["size"] = str(len(data))
    else:
        resource["version"] = str(int(resource["version"]) + 1)


def problem(response: httpx.Response, status: int, code: str) -> None:
    assert response.status_code == status, response.text
    assert response.json()["code"] == code, response.text


async def connected_source(env: Env, folder: str | None, **extra: Any) -> str:
    """A Drive source of the KB, connected through OAuth and (with ``folder``) active on that folder."""
    created = await env.admin.post(
        URL, json={"type": "google_drive", "name": "Team", "knowledge_base_id": env.kb_id, **extra}
    )
    assert created.status_code == 201, created.text
    source_id = str(created.json()["id"])
    started = await env.admin.post(f"{URL}/{source_id}/oauth/start")
    code, state = env.google.authorize(started.json()["authorization_url"])
    callback = await env.admin.get(f"{URL}/oauth/callback", params={"state": state, "code": code})
    assert "oauth=connected" in callback.headers["location"], callback.headers["location"]
    if folder is not None:
        patched = await env.admin.patch(f"{URL}/{source_id}", json={"folder_id": folder})
        assert patched.status_code == 200, patched.text
        assert patched.json()["status"] == "active"
    return source_id


async def jobs_of(env: Env, source_id: str) -> list[Any]:
    return await env.harness.sql(
        "SELECT id, trigger, mode, status, error_code, stats, attempt FROM sync_jobs "
        "WHERE data_source_id = CAST(:id AS uuid) ORDER BY queued_at DESC, id",
        id=source_id,
    )


async def active_job(env: Env, source_id: str) -> str:
    [job] = [row for row in await jobs_of(env, source_id) if row[3] in ("queued", "running")]
    return str(job[0])


async def run_sync(
    env: Env, job_id: str, *, attempts: int = 1, max_bytes: int | None = None
) -> SyncRunResult:
    """What the ``connectors`` task does, against the harness database, storage and network policy."""
    settings, resources = env.harness.settings, env.harness.resources
    engine = create_engine(settings, application_name="plummy-test-worker")
    try:
        runner = RunSyncJob(
            SqlAlchemyUnitOfWorkFactory(create_session_factory(engine)),
            resources.storage,
            SystemClock(),
            resources.secrets,
            oauth_clients(settings, resources.http),
            source_connectors(settings, resources.http, max_attempts=attempts),
            resources.jobs,
            max_bytes=max_bytes or settings.upload_max_bytes,
        )
        return await runner.execute(UUID(job_id))
    finally:
        await engine.dispose()


async def sync_now(env: Env, source_id: str, mode: str = "incremental") -> SyncRunResult:
    response = await env.admin.post(f"{URL}/{source_id}/sync", json={"mode": mode})
    assert response.status_code == 202, response.text
    return await run_sync(env, str(response.json()["id"]))


async def documents(env: Env, source_id: str) -> dict[str, Any]:
    """Live documents by external ID: (filename, etag, ancestors, versions)."""
    rows = await env.harness.sql(
        "SELECT d.external_id, d.filename, d.external_etag, d.source_metadata->>'ancestors', "
        "(SELECT count(*) FROM document_versions v WHERE v.document_id = d.id) "
        "FROM documents d WHERE d.data_source_id = CAST(:id AS uuid) AND d.deleted_at IS NULL",
        id=source_id,
    )
    return {row[0]: row[1:] for row in rows}


async def deleted(env: Env, source_id: str) -> dict[str, str]:
    rows = await env.harness.sql(
        "SELECT external_id, deletion_reason FROM documents "
        "WHERE data_source_id = CAST(:id AS uuid) AND deleted_at IS NOT NULL",
        id=source_id,
    )
    return {row[0]: row[1] for row in rows}


async def source_row(env: Env, source_id: str) -> Any:
    [row] = await env.harness.sql(
        "SELECT status, sync_cursor, last_sync_status, last_error, next_sync_at, last_reconciled_at, "
        "last_sync_at FROM data_sources WHERE id = CAST(:id AS uuid)",
        id=source_id,
    )
    return row


async def initial(env: Env, tree: Tree) -> str:
    source_id = await connected_source(env, tree.folder)
    result = await run_sync(env, await active_job(env, source_id))
    assert result.outcome is SyncRunOutcome.SUCCEEDED
    return source_id


def stats(job: Any) -> dict[str, int]:
    return {key: value for key, value in job[5].items() if value}


# --- activation and the initial sync ----------------------------------------------------------------------


async def test_choosing_a_folder_queues_an_initial_sync(env: Env) -> None:
    tree = build_tree(env.google)
    source_id = await connected_source(env, tree.folder)
    [job] = await jobs_of(env, source_id)
    assert job[1:4] == ("initial", "initial", "queued")
    [message] = env.harness.queued_messages(CONNECTORS_QUEUE)
    assert message["headers"]["task"] == SYNC_TASK
    assert message["headers"]["argsrepr"] == repr([str(job[0])])  # the job ID only, never a token


async def test_initial_sync_takes_in_the_folder_tree(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    tree = build_tree(env.google)
    source_id = await initial(env, tree)

    docs = await documents(env, source_id)
    assert set(docs) == {tree.alpha, tree.beta, tree.plan}  # not the shortcut, the form or the outside file
    assert docs[tree.alpha][0] == "alpha.txt"
    assert docs[tree.plan][0] == "Plan.docx"  # the Google Doc as its DOCX export
    assert docs[tree.beta][2] == f"/{tree.folder}/{tree.sub}/"
    assert docs[tree.alpha][1].startswith("md5:")
    assert docs[tree.plan][1] == "v:1"
    [job] = await jobs_of(env, source_id)
    assert job[3] == "succeeded"
    assert stats(job) == {"added": 3, "skipped": 2}

    status, cursor, last_status, last_error, next_sync_at, reconciled, last_sync = await source_row(
        env, source_id
    )
    assert (status, cursor, last_status, last_error) == ("active", "0", "succeeded", None)
    assert reconciled is not None
    assert next_sync_at - last_sync == timedelta(minutes=15)

    versions = await env.harness.sql("SELECT external_version, status FROM document_versions ORDER BY 1")
    assert {row[1] for row in versions} == {"pending"}
    ingest = await env.harness.sql("SELECT kind, status, stage FROM ingestion_jobs")
    assert sorted(ingest) == [("ingest", "queued", "fetch")] * 3
    assert len(env.harness.queued_messages(INGESTION_QUEUE)) == 3  # parsed by worker-ingestion

    secrets = [*env.google.access_tokens, *env.google.refresh_tokens]
    stored = json.dumps(await env.harness.sql("SELECT * FROM sync_jobs"), default=str)
    stored += json.dumps(await env.harness.sql("SELECT action, details::text FROM audit_logs"), default=str)
    assert not [value for value in secrets if value in stored + caplog.text]
    assert "alpha.txt" not in caplog.text
    assert "Alpha policy" not in caplog.text


async def test_size_limit_skips_files_without_failing_the_sync(env: Env) -> None:
    folder = env.google.folder("Big")
    big = env.google.add("big.txt", parent=folder, content=b"x" * 5000)
    export = env.google.add("Huge", parent=folder, mime=DOC, content=b"PK\x03\x04" + b"y" * 5000)
    small = env.google.add("small.txt", parent=folder, content=b"fits\n")
    source_id = await connected_source(env, folder)
    result = await run_sync(env, await active_job(env, source_id), max_bytes=1000)
    assert result.outcome is SyncRunOutcome.SUCCEEDED
    assert set(await documents(env, source_id)) == {small}
    downloads = [r.target for r in env.server.requests if "alt=media" in r.target or "/export" in r.target]
    assert not [target for target in downloads if big in target]  # the known size is checked first
    assert [
        target for target in downloads if export in target
    ]  # an export has no size: limited while streaming
    assert stats((await jobs_of(env, source_id))[0]) == {"added": 1, "skipped": 2}
    assert await env.harness.sql("SELECT count(*) FROM document_versions") == [(1,)]


# --- incremental changes ----------------------------------------------------------------------------------


async def test_incremental_sync_applies_each_kind_of_change(env: Env) -> None:
    tree = build_tree(env.google)
    source_id = await initial(env, tree)
    set_content(env.google, tree.alpha, b"Alpha policy: leave is 30 days.\n")
    env.google.change(tree.alpha)
    env.google.files[tree.beta]["name"] = "beta-renamed.md"
    env.google.change(tree.beta)
    env.google.files[tree.plan]["trashed"] = True
    env.google.change(tree.plan)
    fresh = env.google.add("gamma.txt", parent=tree.sub, content=b"Gamma.\n")
    env.google.change(fresh)
    env.google.change(tree.outside)  # changes outside the folder are ignored

    assert (await sync_now(env, source_id)).outcome is SyncRunOutcome.SUCCEEDED
    docs = await documents(env, source_id)
    assert set(docs) == {tree.alpha, tree.beta, fresh}
    assert docs[tree.alpha][3] == 2  # a new version for the new content
    assert (docs[tree.beta][0], docs[tree.beta][3]) == ("beta-renamed.md", 1)  # metadata only
    assert await deleted(env, source_id) == {tree.plan: "remote_deleted"}
    job = (await jobs_of(env, source_id))[0]
    assert (job[1], job[2], stats(job)) == (
        "manual",
        "incremental",
        {"added": 1, "updated": 1, "renamed": 1, "deleted": 1},
    )
    assert (await source_row(env, source_id))[1] == str(len(env.google.changes))  # checkpointed cursor
    purges = [m for m in env.harness.queued_messages(MAINTENANCE_QUEUE) if m["headers"]["task"] == PURGE_TASK]
    assert len(purges) == 1

    # Nothing changed since: the next sync does nothing.
    assert (await sync_now(env, source_id)).outcome is SyncRunOutcome.SUCCEEDED
    assert stats((await jobs_of(env, source_id))[0]) == {}
    assert await env.harness.sql("SELECT count(*) FROM document_versions") == [(5,)]


async def test_files_that_leave_the_folder_are_deleted(env: Env) -> None:
    tree = build_tree(env.google)
    source_id = await initial(env, tree)
    env.google.files[tree.alpha]["parents"] = ["root"]
    env.google.change(tree.alpha)
    env.google.change(tree.beta, removed=True)  # no longer visible to the account
    await sync_now(env, source_id)
    assert await deleted(env, source_id) == {tree.alpha: "out_of_scope", tree.beta: "remote_deleted"}


async def test_a_folder_moved_out_reconciles_and_one_moved_in_is_listed(env: Env) -> None:
    tree = build_tree(env.google)
    source_id = await initial(env, tree)
    env.google.files[tree.sub]["parents"] = ["root"]  # Drive reports only the folder itself
    env.google.change(tree.sub)
    await sync_now(env, source_id)
    assert await deleted(env, source_id) == {tree.beta: "out_of_scope"}
    assert (await jobs_of(env, source_id))[0][5]["deleted"] == 1

    env.google.files[tree.sub]["parents"] = [tree.folder]
    env.google.change(tree.sub)
    await sync_now(env, source_id)
    docs = await documents(env, source_id)
    assert tree.beta in docs
    assert docs[tree.beta][2] == f"/{tree.folder}/{tree.sub}/"


async def test_a_folder_moved_inside_the_scope_updates_the_paths(env: Env) -> None:
    tree = build_tree(env.google)
    other = env.google.folder("Archive", parent=tree.folder)
    source_id = await initial(env, tree)
    env.google.files[tree.sub]["parents"] = [other]
    env.google.change(tree.sub)
    await sync_now(env, source_id)
    assert await deleted(env, source_id) == {}
    assert (await documents(env, source_id))[tree.beta][2] == f"/{tree.folder}/{other}/{tree.sub}/"


async def test_reverted_content_indexes_the_older_version_again(env: Env) -> None:
    tree = build_tree(env.google)
    source_id = await initial(env, tree)
    set_content(env.google, tree.alpha, b"Changed.\n")
    env.google.change(tree.alpha)
    await sync_now(env, source_id)
    set_content(env.google, tree.alpha, ALPHA)
    env.google.change(tree.alpha)
    await sync_now(env, source_id)
    assert (await documents(env, source_id))[tree.alpha][3] == 2  # no third copy of the same content
    rows = await env.harness.sql(
        "SELECT j.kind, j.status, j.error_code, v.version_number FROM ingestion_jobs j "
        "JOIN document_versions v ON v.id = j.document_version_id "
        "JOIN documents d ON d.id = j.document_id WHERE d.external_id = :ext ORDER BY j.queued_at",
        ext=tree.alpha,
    )
    assert rows == [
        ("ingest", "cancelled", "superseded_by_sync", 1),
        ("ingest", "cancelled", "superseded_by_sync", 2),
        ("reindex", "queued", None, 1),
    ]


async def test_a_restored_file_gets_a_new_document(env: Env) -> None:
    tree = build_tree(env.google)
    source_id = await initial(env, tree)
    env.google.files[tree.alpha]["trashed"] = True
    env.google.change(tree.alpha)
    await sync_now(env, source_id)
    env.google.files[tree.alpha]["trashed"] = False
    env.google.change(tree.alpha)
    await sync_now(env, source_id)
    assert tree.alpha in await documents(env, source_id)
    rows = await env.harness.sql("SELECT count(*) FROM documents WHERE external_id = :ext", ext=tree.alpha)
    assert rows == [(2,)]  # the deleted one waits for its purge beside the live one


async def test_a_refused_cursor_turns_into_a_reconciliation(env: Env) -> None:
    tree = build_tree(env.google)
    source_id = await initial(env, tree)
    await env.harness.sql(
        "UPDATE data_sources SET sync_cursor = '999', last_reconciled_at = NULL WHERE id = CAST(:id AS uuid)",
        id=source_id,
    )
    env.google.files[tree.alpha]["parents"] = ["root"]  # never reported as a change
    assert (await sync_now(env, source_id)).outcome is SyncRunOutcome.SUCCEEDED
    assert await deleted(env, source_id) == {tree.alpha: "out_of_scope"}
    _, cursor, _, _, _, reconciled, _ = await source_row(env, source_id)
    assert cursor == "0"
    assert reconciled is not None


# --- the pipeline and search ------------------------------------------------------------------------------


async def _process(env: Env) -> None:
    """Run the queued ingestion jobs through both phases with the test parser and embedder."""
    settings, resources = env.harness.settings, env.harness.resources
    engine = create_engine(settings, application_name="plummy-test-worker")
    uow = SqlAlchemyUnitOfWorkFactory(create_session_factory(engine))
    try:
        for (job_id,) in await env.harness.sql("SELECT id FROM ingestion_jobs WHERE status = 'queued'"):
            extract = RunIngestionJob(
                uow,
                resources.storage,
                SystemClock(),
                ParserRegistry([PlainTextParser(), StubOfficeParser()]),
                resources.jobs,
            )
            assert await extract.execute(job_id) is RunOutcome.HANDED_OFF
            index = RunIndexingJob(
                uow,
                resources.storage,
                SystemClock(),
                chunker=StructuredChunker(),
                gateway=FakeGateway(FakeEmbedder()),  # type: ignore[arg-type]
                vault=resources.secrets,
                indexes=PgVectorIndexManager(engine),
                jobs=resources.jobs,
            )
            assert await index.execute(job_id) is RunOutcome.SUCCEEDED
    finally:
        await engine.dispose()


async def _keyword_hits(env: Env, source_id: str, query: str) -> int:
    engine = create_engine(env.harness.settings, application_name="plummy-test")
    try:
        async with SqlAlchemyUnitOfWorkFactory(create_session_factory(engine))() as uow:
            kb = await uow.knowledge_bases.get(UUID(env.kb_id))
            assert kb is not None
            profile = await uow.embedding_profiles.get(kb.embedding_profile_id)
            assert profile is not None
            scope = RetrievalScope(frozenset({kb.id}), frozenset({UUID(source_id)}), profile)
            hits = await uow.search.by_keywords(scope, query, limit=10)
            return len(hits or [])
    finally:
        await engine.dispose()


async def test_a_synced_file_is_searchable_until_it_is_deleted_in_drive(env: Env) -> None:
    folder = env.google.folder("Policies")
    alpha = env.google.add("alpha.txt", parent=folder, content=ALPHA)
    source_id = await connected_source(env, folder)
    await run_sync(env, await active_job(env, source_id))
    await _process(env)
    assert await _keyword_hits(env, source_id, "leave days") == 1

    env.google.files[alpha]["trashed"] = True
    env.google.change(alpha)
    await sync_now(env, source_id)
    assert await _keyword_hits(env, source_id, "leave days") == 0  # out of retrieval before the purge


# --- failures -------------------------------------------------------------------------------------------


async def test_a_revoked_grant_fails_the_job_and_stops_the_schedule(env: Env) -> None:
    tree = build_tree(env.google)
    source_id = await connected_source(env, tree.folder)
    env.google.refresh_tokens.clear()  # the user withdrew the consent
    result = await run_sync(env, await active_job(env, source_id))
    assert result.outcome is SyncRunOutcome.FAILED
    job = (await jobs_of(env, source_id))[0]
    assert (job[3], job[4]) == ("failed", "connector_auth_revoked")
    status, _, last_status, last_error, next_sync_at, _, _ = await source_row(env, source_id)
    assert (status, last_status, last_error, next_sync_at) == (
        "error",
        "failed",
        "connector_auth_revoked",
        None,
    )
    assert await env.harness.sql("SELECT status FROM data_source_credentials") == [("revoked",)]
    problem(await env.admin.post(f"{URL}/{source_id}/sync"), 409, "connector_auth_revoked")


async def test_a_rejected_access_token_is_refreshed_once(env: Env) -> None:
    tree = build_tree(env.google)
    source_id = await connected_source(env, tree.folder)
    before = len(env.google.token_requests)  # the folder check of PATCH refreshed once already
    env.google.api_failures.append(Reply.json({"error": {"code": 401}}, status=401))
    assert (await run_sync(env, await active_job(env, source_id))).outcome is SyncRunOutcome.SUCCEEDED
    refreshes = [r for r in env.google.token_requests[before:] if r["grant_type"] == "refresh_token"]
    assert len(refreshes) == 2  # the job's token, then one more after the 401


async def test_rate_limits_retry_the_job_until_the_attempts_run_out(env: Env) -> None:
    tree = build_tree(env.google)
    source_id = await connected_source(env, tree.folder)
    job_id = await active_job(env, source_id)
    limited = Reply.json({"error": {"code": 429}}, status=429, **{"Retry-After": "42"})
    for attempt in range(1, 6):
        env.google.api_failures.append(limited)
        result = await run_sync(env, job_id)
        job = (await jobs_of(env, source_id))[0]
        if attempt < 5:
            assert result == SyncRunResult(SyncRunOutcome.RETRY, 42.0)
            assert (job[3], job[4], job[6]) == ("queued", "connector_rate_limited", attempt)
    assert result.outcome is SyncRunOutcome.DEAD
    assert (job[3], job[4]) == ("dead", "attempts_exhausted")
    status, _, last_status, _, next_sync_at, _, _ = await source_row(env, source_id)
    assert (status, last_status) == ("active", "failed")
    assert next_sync_at is not None


async def test_an_exhausted_quota_fails_the_job_but_not_the_source(env: Env) -> None:
    tree = build_tree(env.google)
    source_id = await connected_source(env, tree.folder)
    env.google.api_failures.append(
        Reply.json({"error": {"code": 403, "errors": [{"reason": "dailyLimitExceeded"}]}}, status=403)
    )
    assert (await run_sync(env, await active_job(env, source_id))).outcome is SyncRunOutcome.FAILED
    status, _, last_status, last_error, next_sync_at, _, _ = await source_row(env, source_id)
    assert (status, last_status, last_error) == ("active", "failed", "connector_quota_exceeded")
    assert next_sync_at is not None


async def test_disconnect_and_another_folder_cancel_the_queued_job(env: Env) -> None:
    tree = build_tree(env.google)
    source_id = await connected_source(env, tree.folder)
    first = await active_job(env, source_id)
    other = env.google.folder("Other")
    assert (await env.admin.patch(f"{URL}/{source_id}", json={"folder_id": other})).status_code == 200
    second = await active_job(env, source_id)
    assert second != first
    assert (await run_sync(env, first)).outcome is SyncRunOutcome.SKIPPED
    assert (await env.admin.post(f"{URL}/{source_id}/disconnect")).status_code == 200
    assert (await run_sync(env, second)).outcome is SyncRunOutcome.SKIPPED
    rows = {str(row[0]): (row[3], row[4]) for row in await jobs_of(env, source_id)}
    assert rows == {
        first: ("cancelled", "source_reconfigured"),
        second: ("cancelled", "source_disconnected"),
    }
    assert await documents(env, source_id) == {}


# --- the API ----------------------------------------------------------------------------------------------


async def test_manual_sync_api(env: Env) -> None:
    tree = build_tree(env.google)
    source_id = await initial(env, tree)
    response = await env.admin.post(f"{URL}/{source_id}/sync", json={"mode": "reconciliation"})
    assert response.status_code == 202, response.text
    body = response.json()
    assert {key: body[key] for key in ("type", "trigger", "mode", "status", "error")} == {
        "type": "sync",
        "trigger": "reconciliation",
        "mode": "reconciliation",
        "status": "queued",
        "error": None,
    }
    problem(await env.admin.post(f"{URL}/{source_id}/sync"), 409, "sync_in_progress")
    problem(
        await env.admin.post(f"{URL}/{source_id}/sync", json={"mode": "initial"}), 422, "validation_error"
    )
    problem(
        await env.admin.post(f"{URL}/{source_id}/sync", json={"mode": "x", "y": 1}), 422, "validation_error"
    )
    audit = await env.harness.sql("SELECT details FROM audit_logs WHERE action = 'sync.requested'")
    assert [row[0]["mode"] for row in audit] == ["reconciliation"]

    await active_user(env.harness, env.admin, "member@example.com", ["member"])
    async with env.harness.new_client() as member:
        assert (await login(member, "member@example.com")).status_code == 200
        problem(await member.post(f"{URL}/{source_id}/sync"), 403, "permission_denied")
    await active_user(env.harness, env.admin, "km@example.com", ["knowledge_manager"])
    await env.harness.sql("UPDATE sync_jobs SET status = 'succeeded'")
    async with env.harness.new_client() as manager:
        assert (await login(manager, "km@example.com")).status_code == 200
        assert (await manager.post(f"{URL}/{source_id}/sync")).status_code == 202  # sync without manage

    for _ in range(4):  # 6 per hour and source, counted from the first manual request
        await env.harness.sql("UPDATE sync_jobs SET status = 'succeeded'")
        assert (await env.admin.post(f"{URL}/{source_id}/sync")).status_code == 202
    await env.harness.sql("UPDATE sync_jobs SET status = 'succeeded'")
    limited = await env.admin.post(f"{URL}/{source_id}/sync")
    problem(limited, 429, "rate_limited")
    assert int(limited.headers["retry-after"]) > 0


async def test_sync_refuses_sources_that_cannot_sync(env: Env) -> None:
    unconnected = await env.admin.post(
        URL, json={"type": "google_drive", "name": "New", "knowledge_base_id": env.kb_id}
    )
    problem(await env.admin.post(f"{URL}/{unconnected.json()['id']}/sync"), 409, "source_not_connected")
    without_folder = await connected_source(env, None)
    problem(await env.admin.post(f"{URL}/{without_folder}/sync"), 409, "source_not_active")
    sources = (await env.admin.get(URL)).json()["items"]
    [upload] = [s for s in sources if s["type"] == "upload"]
    problem(await env.admin.post(f"{URL}/{upload['id']}/sync"), 409, "upload_source_managed_by_kb")
    missing = "00000000-0000-4000-8000-000000000000"
    problem(await env.admin.post(f"{URL}/{missing}/sync"), 404, "not_found")
    assert await env.harness.sql("SELECT count(*) FROM sync_jobs") == [(0,)]


# --- the scheduler ----------------------------------------------------------------------------------------


async def schedule(env: Env) -> int:
    engine = create_engine(env.harness.settings, application_name="plummy-test-worker")
    try:
        uow = SqlAlchemyUnitOfWorkFactory(create_session_factory(engine))
        return await ScheduleSyncs(uow, env.harness.resources.jobs, SystemClock()).execute()
    finally:
        await engine.dispose()


async def test_the_scheduler_queues_due_sources_and_reconciles_daily(env: Env) -> None:
    tree = build_tree(env.google)
    source_id = await initial(env, tree)
    assert await schedule(env) == 0  # not due yet
    past = datetime.now(UTC) - timedelta(minutes=1)
    await env.harness.sql(
        "UPDATE data_sources SET next_sync_at = :at WHERE id = CAST(:id AS uuid)", at=past, id=source_id
    )
    assert await schedule(env) == 1
    assert await schedule(env) == 0  # one active job per source
    job = (await jobs_of(env, source_id))[0]
    assert job[1:4] == ("schedule", "incremental", "queued")

    await run_sync(env, str(job[0]))
    yesterday = datetime.now(UTC) - timedelta(days=1, minutes=1)
    await env.harness.sql(
        "UPDATE data_sources SET next_sync_at = :at, last_reconciled_at = :old WHERE id = CAST(:id AS uuid)",
        at=past,
        old=yesterday,
        id=source_id,
    )
    assert await schedule(env) == 1
    assert (await jobs_of(env, source_id))[0][1:4] == ("reconciliation", "reconciliation", "queued")


async def test_the_scheduler_skips_manual_and_paused_sources(env: Env) -> None:
    tree = build_tree(env.google)
    manual = await connected_source(env, tree.folder, sync_interval_minutes=None)
    await run_sync(env, await active_job(env, manual))
    assert (await source_row(env, manual))[4] is None  # manual only: no next sync
    past = datetime.now(UTC) - timedelta(minutes=1)
    await env.harness.sql("UPDATE data_sources SET status = 'paused', next_sync_at = :at", at=past)
    assert await schedule(env) == 0


async def test_the_scheduler_recovers_lost_and_stalled_jobs(env: Env) -> None:
    tree = build_tree(env.google)
    source_id = await connected_source(env, tree.folder)
    job_id = await active_job(env, source_id)
    long_ago = datetime.now(UTC) - timedelta(hours=3)
    await env.harness.sql(
        "UPDATE sync_jobs SET status = 'running', attempt = 1, updated_at = :at", at=long_ago
    )
    await schedule(env)
    assert (await jobs_of(env, source_id))[0][3:5] == ("queued", "transient_error")
    await env.harness.sql("UPDATE sync_jobs SET updated_at = :at", at=long_ago)  # never reached the broker
    before = len(env.harness.queued_messages(CONNECTORS_QUEUE))
    await schedule(env)
    assert len(env.harness.queued_messages(CONNECTORS_QUEUE)) == before + 1
    assert (await run_sync(env, job_id)).outcome is SyncRunOutcome.SUCCEEDED
