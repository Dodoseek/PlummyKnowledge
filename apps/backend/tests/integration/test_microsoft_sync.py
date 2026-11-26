"""Sync of a Microsoft 365 source (ADR-021 §7) against a fake Graph on loopback, real PostgreSQL and Redis.

The runner is the one the ``connectors`` task builds: the real ``MicrosoftOAuthClient`` and
``MicrosoftGraphConnector`` through the app's ``SafeHttpClient`` and network policy, the real vault and
storage, and the same sync engine as Google Drive: Delta Query on the whole drive, filtered to the chosen
folder.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from plummy.application.sources.sync import SyncRunOutcome, SyncRunResult
from tests.fake_http import FakeHttpServer, Reply, serve
from tests.fake_microsoft import CLIENT_ID, CLIENT_SECRET, TENANT, FakeMicrosoft, connector_config
from tests.integration.harness import Harness, running_auth_app
from tests.integration.rbac import setup_admin_client
from tests.integration.test_source_sync import (
    _keyword_hits,
    _process,
    active_job,
    deleted,
    documents,
    jobs_of,
    run_sync,
    source_row,
    stats,
)

pytestmark = pytest.mark.integration

URL = "/api/v1/data-sources"
ALPHA = b"Alpha policy: leave is 28 days.\n"
BETA = b"# Beta\n\nBeta contract terms.\n"


@dataclass
class Env:
    harness: Harness
    admin: httpx.AsyncClient
    microsoft: FakeMicrosoft
    server: FakeHttpServer
    kb_id: str


@dataclass
class Tree:
    drive: str
    folder: str
    sub: str
    alpha: str
    beta: str
    outside: str

    def ref(self, item_id: str) -> str:
        return f"item:{self.drive}:{item_id}"


@pytest.fixture
async def env(
    migrated_database_url: str, redis_url: str, clean_rate_limits: None, tmp_path: Path
) -> AsyncIterator[Env]:
    microsoft = FakeMicrosoft()
    async with serve(microsoft.handle) as server:
        config = tmp_path / "plummy.toml"
        config.write_text(connector_config(server.port))
        async with running_auth_app(
            migrated_database_url,
            redis_url,
            tmp_path / "bootstrap",
            config_file=config,
            microsoft_oauth_tenant_id=TENANT,
            microsoft_oauth_client_id=CLIENT_ID,
            microsoft_oauth_client_secret=CLIENT_SECRET,
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
                json={"name": "Legal", "embedding_profile_id": profile.json()["id"]},
            )
            assert kb.status_code == 201, kb.text
            yield Env(harness, admin, microsoft, server, str(kb.json()["id"]))


def build_tree(microsoft: FakeMicrosoft) -> Tree:
    drive = microsoft.drive("Documents", site=microsoft.site("Legal"))
    folder = microsoft.folder(drive, "Contracts")
    sub = microsoft.folder(drive, "2026", parent=folder)
    alpha = microsoft.add(drive, "alpha.txt", parent=folder, content=ALPHA)
    beta = microsoft.add(drive, "beta.md", parent=sub, content=BETA)
    microsoft.add(drive, "Notebook", parent=folder, package={"type": "oneNote"})
    microsoft.add(drive, "Shared.txt", parent=folder, remoteItem={"id": "01REMOTE"}, file={})
    outside = microsoft.add(drive, "outside.txt", content=b"Not in the folder.\n")
    return Tree(drive, folder, sub, alpha, beta, outside)


async def connected_source(env: Env, folder: str | None) -> str:
    """A Microsoft 365 source of the KB, connected through OAuth and (with ``folder``) active on it."""
    created = await env.admin.post(
        URL, json={"type": "microsoft_365", "name": "Legal", "knowledge_base_id": env.kb_id}
    )
    assert created.status_code == 201, created.text
    source_id = str(created.json()["id"])
    started = await env.admin.post(f"{URL}/{source_id}/oauth/start")
    code, state = env.microsoft.authorize(started.json()["authorization_url"])
    callback = await env.admin.get(f"{URL}/oauth/callback", params={"state": state, "code": code})
    assert "oauth=connected" in callback.headers["location"], callback.headers["location"]
    if folder is not None:
        patched = await env.admin.patch(f"{URL}/{source_id}", json={"folder_id": folder})
        assert patched.status_code == 200, patched.text
        assert patched.json()["status"] == "active"
    return source_id


async def sync_now(env: Env, source_id: str, mode: str = "incremental") -> SyncRunResult:
    response = await env.admin.post(f"{URL}/{source_id}/sync", json={"mode": mode})
    assert response.status_code == 202, response.text
    return await run_sync(env, str(response.json()["id"]))


async def initial(env: Env, tree: Tree) -> str:
    source_id = await connected_source(env, tree.ref(tree.folder))
    result = await run_sync(env, await active_job(env, source_id))
    assert result.outcome is SyncRunOutcome.SUCCEEDED
    return source_id


def log_cursor(microsoft: FakeMicrosoft, drive: str) -> str:
    return f"token={len(microsoft.log[drive])}"


# --- the initial sync -------------------------------------------------------------------------------------


async def test_initial_sync_takes_in_the_folder_tree(env: Env, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    tree = build_tree(env.microsoft)
    source_id = await initial(env, tree)

    docs = await documents(env, source_id)
    assert set(docs) == {
        tree.ref(tree.alpha),
        tree.ref(tree.beta),
    }  # no notebook, remote item or outside file
    assert docs[tree.ref(tree.alpha)][0] == "alpha.txt"
    assert docs[tree.ref(tree.alpha)][1].startswith("qx:")
    assert docs[tree.ref(tree.beta)][2] == f"/{tree.ref(tree.folder)}/{tree.ref(tree.sub)}/"
    [job] = await jobs_of(env, source_id)
    assert (job[2], job[3], stats(job)) == ("initial", "succeeded", {"added": 2, "skipped": 2})

    status, cursor, last_status, last_error, _, reconciled, _ = await source_row(env, source_id)
    assert (status, cursor, last_status, last_error) == (
        "active",
        log_cursor(env.microsoft, tree.drive),
        "succeeded",
        None,
    )
    assert reconciled is not None
    deltas = [request.target for request in env.server.requests if "/root/delta" in request.target]
    assert len(deltas) == 1
    assert "token=latest" in deltas[0]  # the start of the feed, taken before the listing, without enumerating

    secrets = [*env.microsoft.access_tokens, *env.microsoft.refresh_tokens]
    stored = json.dumps(await env.harness.sql("SELECT * FROM sync_jobs"), default=str)
    stored += json.dumps(await env.harness.sql("SELECT action, details::text FROM audit_logs"), default=str)
    assert not [value for value in secrets if value in stored + caplog.text]
    assert "alpha.txt" not in caplog.text
    assert "Alpha policy" not in caplog.text


async def test_a_whole_library_can_be_synced(env: Env) -> None:
    tree = build_tree(env.microsoft)
    source_id = await connected_source(env, f"drive:{tree.drive}")
    assert (await run_sync(env, await active_job(env, source_id))).outcome is SyncRunOutcome.SUCCEEDED
    docs = await documents(env, source_id)
    assert set(docs) == {tree.ref(tree.alpha), tree.ref(tree.beta), tree.ref(tree.outside)}
    root = tree.ref(env.microsoft.root(tree.drive))
    assert docs[tree.ref(tree.outside)][2] == f"/{root}/"


# --- incremental changes ----------------------------------------------------------------------------------


async def test_incremental_sync_applies_each_kind_of_change(env: Env) -> None:
    microsoft = env.microsoft
    tree = build_tree(microsoft)
    source_id = await initial(env, tree)
    microsoft.set_content(tree.drive, tree.alpha, b"Alpha policy: leave is 30 days.\n")
    microsoft.change(tree.drive, tree.beta, name="beta-renamed.md")
    gone = microsoft.add(tree.drive, "gone.txt", parent=tree.folder, content=b"Soon gone.\n")
    fresh = microsoft.add(tree.drive, "gamma.txt", parent=tree.sub, content=b"Gamma.\n")
    microsoft.change(tree.drive, tree.outside, name="outside-renamed.txt")  # outside the folder: ignored
    microsoft.delete(tree.drive, gone)
    microsoft.page_size = 2  # several delta pages, each one a checkpoint

    assert (await sync_now(env, source_id)).outcome is SyncRunOutcome.SUCCEEDED
    docs = await documents(env, source_id)
    assert set(docs) == {tree.ref(tree.alpha), tree.ref(tree.beta), tree.ref(fresh)}
    assert docs[tree.ref(tree.alpha)][3] == 2  # a new version for the new content
    assert (docs[tree.ref(tree.beta)][0], docs[tree.ref(tree.beta)][3]) == (
        "beta-renamed.md",
        1,
    )  # metadata only
    assert await deleted(env, source_id) == {}  # "gone" was never indexed
    job = (await jobs_of(env, source_id))[0]
    assert (job[2], stats(job)) == ("incremental", {"added": 1, "updated": 1, "renamed": 1})
    assert (await source_row(env, source_id))[1] == log_cursor(microsoft, tree.drive)

    microsoft.delete(tree.drive, tree.alpha)
    assert (await sync_now(env, source_id)).outcome is SyncRunOutcome.SUCCEEDED
    assert await deleted(env, source_id) == {tree.ref(tree.alpha): "remote_deleted"}

    # Nothing changed since: the next sync does nothing.
    assert (await sync_now(env, source_id)).outcome is SyncRunOutcome.SUCCEEDED
    assert stats((await jobs_of(env, source_id))[0]) == {}


async def test_files_that_leave_the_folder_are_deleted(env: Env) -> None:
    microsoft = env.microsoft
    tree = build_tree(microsoft)
    source_id = await initial(env, tree)
    root = microsoft.root(tree.drive)
    microsoft.change(tree.drive, tree.alpha, parentReference={"driveId": tree.drive, "id": root})
    await sync_now(env, source_id)
    assert await deleted(env, source_id) == {tree.ref(tree.alpha): "out_of_scope"}


async def test_a_folder_moved_out_reconciles_and_one_moved_in_is_listed(env: Env) -> None:
    microsoft = env.microsoft
    tree = build_tree(microsoft)
    source_id = await initial(env, tree)
    root = microsoft.root(tree.drive)
    microsoft.change(tree.drive, tree.sub, parentReference={"driveId": tree.drive, "id": root})
    await sync_now(env, source_id)  # only the folder is reported
    assert await deleted(env, source_id) == {tree.ref(tree.beta): "out_of_scope"}

    microsoft.change(tree.drive, tree.sub, parentReference={"driveId": tree.drive, "id": tree.folder})
    await sync_now(env, source_id)
    docs = await documents(env, source_id)
    assert docs[tree.ref(tree.beta)][2] == f"/{tree.ref(tree.folder)}/{tree.ref(tree.sub)}/"


async def test_a_deleted_folder_with_documents_reconciles(env: Env) -> None:
    microsoft = env.microsoft
    tree = build_tree(microsoft)
    source_id = await initial(env, tree)
    microsoft.delete(tree.drive, tree.sub)  # the log holds the folder, not the file in it
    await sync_now(env, source_id)
    assert set(await deleted(env, source_id)) == {tree.ref(tree.beta)}
    _, _, _, _, _, reconciled, _ = await source_row(env, source_id)
    assert reconciled is not None


async def test_a_deleted_synced_folder_fails_the_job_and_keeps_the_documents(env: Env) -> None:
    microsoft = env.microsoft
    tree = build_tree(microsoft)
    source_id = await initial(env, tree)
    microsoft.delete(tree.drive, tree.folder)
    result = await sync_now(env, source_id)
    assert result.outcome is SyncRunOutcome.FAILED  # the reconciliation cannot list the folder
    job = (await jobs_of(env, source_id))[0]
    assert (job[3], job[4]) == ("failed", "connector_not_found")
    assert await deleted(env, source_id) == {}  # nothing is mass-deleted because the folder is gone
    status, _, last_status, last_error, _, _, _ = await source_row(env, source_id)
    assert (status, last_status, last_error) == ("active", "failed", "connector_not_found")
    report = (await env.admin.post(f"{URL}/{source_id}/validate")).json()
    assert (report["status"], report["error_code"]) == ("failed", "folder_not_found")


@pytest.mark.parametrize("cursor", ["token=999", "https://evil.example/delta?token=1", "$deltatoken="])
async def test_a_refused_or_forged_cursor_turns_into_a_reconciliation(env: Env, cursor: str) -> None:
    microsoft = env.microsoft
    tree = build_tree(microsoft)
    source_id = await initial(env, tree)
    await env.harness.sql(
        "UPDATE data_sources SET sync_cursor = :cursor, last_reconciled_at = NULL "
        "WHERE id = CAST(:id AS uuid)",
        cursor=cursor,
        id=source_id,
    )
    root = microsoft.root(tree.drive)
    microsoft.items[(tree.drive, tree.alpha)]["parentReference"]["id"] = root  # never reported as a change
    assert (await sync_now(env, source_id)).outcome is SyncRunOutcome.SUCCEEDED
    assert await deleted(env, source_id) == {tree.ref(tree.alpha): "out_of_scope"}
    _, stored, _, _, _, reconciled, _ = await source_row(env, source_id)
    assert stored == log_cursor(microsoft, tree.drive)
    assert reconciled is not None
    assert not [request for request in env.server.requests if "evil" in request.target]


# --- the pipeline, errors and the grant -------------------------------------------------------------------


async def test_a_synced_file_is_searchable_until_it_is_deleted(env: Env) -> None:
    microsoft = env.microsoft
    drive = microsoft.drive("Documents", site=microsoft.site("Legal"))
    folder = microsoft.folder(drive, "Policies")
    alpha = microsoft.add(drive, "alpha.txt", parent=folder, content=ALPHA)
    source_id = await connected_source(env, f"item:{drive}:{folder}")
    await run_sync(env, await active_job(env, source_id))
    await _process(env)
    assert await _keyword_hits(env, source_id, "leave days") == 1

    microsoft.delete(drive, alpha)
    await sync_now(env, source_id)
    assert await _keyword_hits(env, source_id, "leave days") == 0


async def test_throttling_retries_the_job_with_retry_after(env: Env) -> None:
    tree = build_tree(env.microsoft)
    source_id = await connected_source(env, tree.ref(tree.folder))
    job_id = await active_job(env, source_id)
    env.microsoft.api_failures.append(
        Reply.json({"error": {"code": "activityLimitReached"}}, status=429, **{"Retry-After": "42"})
    )
    result = await run_sync(env, job_id)
    assert result == SyncRunResult(SyncRunOutcome.RETRY, 42.0)
    job = (await jobs_of(env, source_id))[0]
    assert (job[3], job[4]) == ("queued", "connector_rate_limited")
    assert (await run_sync(env, job_id)).outcome is SyncRunOutcome.SUCCEEDED


async def test_a_rotated_refresh_token_is_kept_between_jobs(env: Env) -> None:
    tree = build_tree(env.microsoft)
    source_id = await initial(env, tree)
    for _ in range(2):  # each job refreshes with the token the previous one stored
        assert (await sync_now(env, source_id)).outcome is SyncRunOutcome.SUCCEEDED
    assert len(env.microsoft.refresh_tokens) == 1


@pytest.mark.parametrize("error", ["invalid_grant", "interaction_required"])
async def test_a_dead_grant_fails_the_job_and_stops_the_schedule(env: Env, error: str) -> None:
    tree = build_tree(env.microsoft)
    source_id = await connected_source(env, tree.ref(tree.folder))
    env.microsoft.refresh_error = error
    env.microsoft.revoke_all()
    result = await run_sync(env, await active_job(env, source_id))
    assert result.outcome is SyncRunOutcome.FAILED
    job = (await jobs_of(env, source_id))[0]
    assert (job[3], job[4]) == ("failed", "connector_auth_revoked")
    status, _, _, last_error, next_sync_at, _, _ = await source_row(env, source_id)
    assert (status, last_error, next_sync_at) == ("error", "connector_auth_revoked", None)
