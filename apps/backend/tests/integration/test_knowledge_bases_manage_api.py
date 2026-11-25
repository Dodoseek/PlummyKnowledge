"""Managing knowledge bases: ``PATCH``, ``DELETE``, ``GET``/``PUT …/access`` and ``POST …/reindex`` on real
PostgreSQL, Redis and storage (API_CONTRACTS.md §2.10, SECURITY_MODEL.md §3.5, DATABASE_SCHEMA.md §8)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest

from plummy.workers.app import MAINTENANCE_QUEUE, PURGE_TASK
from tests.integration.harness import running_auth_app
from tests.integration.rbac import audit_actions
from tests.integration.test_document_upload import (
    Env,
    job_row,
    make_env,
    problem,
    signed_in,
    uploaded,
)
from tests.integration.test_documents_api import grant, indexed, maintenance, tasks_in
from tests.integration.test_indexing import counts

pytestmark = pytest.mark.integration

KBS = "/api/v1/knowledge-bases"
DOCS = "/api/v1/documents"
MISSING = "00000000-0000-7000-8000-000000000000"


@pytest.fixture
async def env(
    migrated_database_url: str, redis_url: str, clean_rate_limits: None, tmp_path: Path
) -> AsyncIterator[Env]:
    async with running_auth_app(migrated_database_url, redis_url, tmp_path / "bootstrap") as harness:
        yield await make_env(harness)


async def kb(env: Env, client: httpx.AsyncClient | None = None) -> httpx.Response:
    return await (client or env.admin).get(f"{KBS}/{env.kb_id}")


async def group(env: Env, name: str, members: list[str]) -> str:
    created = await env.admin.post("/api/v1/groups", json={"name": name})
    assert created.status_code == 201, created.text
    group_id: str = created.json()["id"]
    response = await env.admin.put(f"/api/v1/groups/{group_id}/members", json={"user_ids": members})
    assert response.status_code == 200, response.text
    return group_id


def entry(principal_id: str, level: str, principal_type: str = "user") -> dict[str, str]:
    return {"principal_type": principal_type, "principal_id": principal_id, "access_level": level}


async def put_access(
    env: Env, grants: list[dict[str, str]], client: httpx.AsyncClient | None = None
) -> httpx.Response:
    return await (client or env.admin).put(f"{KBS}/{env.kb_id}/access", json={"grants": grants})


async def audit_count(env: Env, action: str) -> int:
    return sum(1 for name in await audit_actions(env.harness) if name == action)


# --- PATCH ------------------------------------------------------------------------------------------------


async def test_rename_and_describe(env: Env) -> None:
    before = (await kb(env)).json()
    response = await env.admin.patch(
        f"{KBS}/{env.kb_id}", json={"name": "  Human   Resources ", "description": " Policies "}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["name"], body["description"]) == ("Human Resources", "Policies")
    assert body["updated_at"] > before["updated_at"]
    assert body["my_access_level"] == "manage"
    assert "indexing" in body
    assert await env.harness.sql(
        "SELECT name FROM data_sources WHERE owner_knowledge_base_id = :kb", kb=env.kb_id
    ) == [("Uploads: Human Resources",)]
    assert await audit_count(env, "kb.updated") == 1

    cleared = await env.admin.patch(f"{KBS}/{env.kb_id}", json={"description": None})
    assert (cleared.json()["name"], cleared.json()["description"]) == ("Human Resources", None)
    assert await audit_count(env, "kb.updated") == 2


async def test_a_patch_without_changes_records_nothing(env: Env) -> None:
    name = (await kb(env)).json()["name"]
    for body in ({}, {"name": name}, {"description": None}):
        response = await env.admin.patch(f"{KBS}/{env.kb_id}", json=body)
        assert response.status_code == 200, response.text
    assert await audit_count(env, "kb.updated") == 0


async def test_patch_validation(env: Env) -> None:
    other = await env.admin.post(
        KBS, json={"name": "Finance", "embedding_profile_id": (await kb(env)).json()["embedding_profile_id"]}
    )
    assert other.status_code == 201
    problem(
        await env.admin.patch(f"{KBS}/{env.kb_id}", json={"name": "FINANCE"}),
        409,
        "knowledge_base_name_taken",
    )
    body = problem(await env.admin.patch(f"{KBS}/{env.kb_id}", json={"name": "   "}), 422, "validation_error")
    assert body["errors"][0]["field"] == "body.name"
    problem(await env.admin.patch(f"{KBS}/{env.kb_id}", json={"tenant": "x"}), 422, "validation_error")
    # The profile is fixed: the same value is accepted, another one is not.
    same = (await kb(env)).json()["embedding_profile_id"]
    assert (
        await env.admin.patch(f"{KBS}/{env.kb_id}", json={"embedding_profile_id": same})
    ).status_code == 200
    body = problem(
        await env.admin.patch(f"{KBS}/{env.kb_id}", json={"embedding_profile_id": MISSING}),
        422,
        "validation_error",
    )
    assert body["errors"][0]["code"] == "embedding_profile_change_not_supported"
    assert (await kb(env)).json()["name"] == "KB"


async def test_patch_needs_manage_on_the_knowledge_base(env: Env) -> None:
    reader, reader_id = await signed_in(env, "reader@example.com", [])
    outsider, _ = await signed_in(env, "outsider@example.com", [])
    manager, manager_id = await signed_in(env, "manager@example.com", [])
    await grant(env, reader_id, "read")
    await grant(env, manager_id, "manage")
    problem(await reader.patch(f"{KBS}/{env.kb_id}", json={"name": "X"}), 403, "permission_denied")
    problem(await outsider.patch(f"{KBS}/{env.kb_id}", json={"name": "X"}), 404, "not_found")
    problem(await env.admin.patch(f"{KBS}/{MISSING}", json={"name": "X"}), 404, "not_found")
    renamed = await manager.patch(f"{KBS}/{env.kb_id}", json={"name": "Renamed"})
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["my_access_level"] == "manage"


# --- DELETE -----------------------------------------------------------------------------------------------


async def test_delete_hides_the_knowledge_base_and_its_documents_at_once(env: Env) -> None:
    _, first = await indexed(env)
    _, second = await indexed(env, "other.md", b"# Other\n\nMore text.")
    reader, reader_id = await signed_in(env, "reader@example.com", [])
    await grant(env, reader_id, "read")
    profile_id = (await kb(env)).json()["embedding_profile_id"]

    response = await env.admin.delete(f"{KBS}/{env.kb_id}")
    assert response.status_code == 204, response.text
    problem(await kb(env), 404, "not_found")
    problem(await kb(env, reader), 404, "not_found")
    assert (await reader.get(KBS)).json()["items"] == []
    for document_id in (first, second):
        problem(await env.admin.get(f"{DOCS}/{document_id}"), 404, "not_found")
    assert await env.harness.sql(
        "SELECT deletion_reason, count(*) FROM documents GROUP BY deletion_reason"
    ) == [("kb_deleted", 2)]
    assert await env.harness.sql("SELECT count(*) FROM knowledge_base_access") == [(0,)]
    assert await env.harness.sql("SELECT count(*) FROM knowledge_base_sources") == [(0,)]
    assert await env.harness.sql("SELECT deleted_at IS NOT NULL FROM data_sources") == [(True,)]
    assert sorted(tasks_in(env, MAINTENANCE_QUEUE, PURGE_TASK)) == sorted([[first], [second]])
    assert await audit_count(env, "kb.deleted") == 1
    problem(await env.admin.delete(f"{KBS}/{env.kb_id}"), 404, "not_found")  # already deleted

    async with maintenance(env) as services:
        assert await services.purge.execute(UUID(first)) is True
        assert await services.purge.execute(UUID(second)) is True
    assert await counts(env, first) == (0, 0)
    # The name is free again.
    again = await env.admin.post(KBS, json={"name": "KB", "embedding_profile_id": profile_id})
    assert again.status_code == 201, again.text


async def test_delete_cancels_unfinished_jobs(env: Env) -> None:
    job_id, _ = await uploaded(env, "policy.md", b"# Policy\n\nText.")
    assert (await env.admin.delete(f"{KBS}/{env.kb_id}")).status_code == 204
    assert await job_row(env, job_id) == ("cancelled", "fetch", None, 0, "kb_deleted")


async def test_only_knowledge_bases_manage_deletes(env: Env) -> None:
    manager, manager_id = await signed_in(env, "manager@example.com", [])
    await grant(env, manager_id, "manage")
    problem(await manager.delete(f"{KBS}/{env.kb_id}"), 403, "permission_denied")
    problem(await env.admin.delete(f"{KBS}/{MISSING}"), 404, "not_found")
    assert (await kb(env)).status_code == 200


# --- access -----------------------------------------------------------------------------------------------


async def test_replace_access_grants_and_revokes_at_once(env: Env) -> None:
    reader, reader_id = await signed_in(env, "reader@example.com", [])
    member, member_id = await signed_in(env, "member@example.com", [])
    team = await group(env, "Team", [member_id])
    problem(await kb(env, reader), 404, "not_found")

    response = await put_access(env, [entry(reader_id, "read"), entry(team, "manage", "group")])
    assert response.status_code == 200, response.text
    items = response.json()["items"]
    assert [(i["principal_type"], i["name"], i["email"], i["access_level"]) for i in items] == [
        ("user", "Invited User", "reader@example.com", "read"),
        ("group", "Team", None, "manage"),
    ]
    assert all(i["granted_at"] is not None for i in items)
    assert (await kb(env, reader)).json()["my_access_level"] == "read"
    assert (await kb(env, member)).json()["my_access_level"] == "manage"
    assert (await env.admin.get(f"{KBS}/{env.kb_id}/access")).json()["items"] == items
    assert await audit_count(env, "kb.access.granted") == 2

    assert (await put_access(env, [])).json()["items"] == []
    problem(await kb(env, reader), 404, "not_found")
    problem(await kb(env, member), 404, "not_found")
    assert await audit_count(env, "kb.access.revoked") == 2


async def test_unchanged_entries_keep_their_grant_and_changes_are_audited(env: Env) -> None:
    _, first = await signed_in(env, "first@example.com", [])
    _, second = await signed_in(env, "second@example.com", [])
    initial = (await put_access(env, [entry(first, "read"), entry(second, "read")])).json()["items"]
    changed = (await put_access(env, [entry(first, "read"), entry(second, "manage")])).json()["items"]
    assert changed[0]["granted_at"] == initial[0]["granted_at"]
    assert changed[1]["access_level"] == "manage"
    assert await audit_count(env, "kb.access.granted") == 3  # two new, one changed level
    assert await audit_count(env, "kb.access.revoked") == 0


@pytest.mark.parametrize(
    ("grants", "field", "code"),
    [
        (
            [entry("{u}", "read"), entry("{u}", "manage")],
            "body.grants.1.principal_id",
            "duplicate_principal",
        ),
        ([entry(MISSING, "read")], "body.grants.0.principal_id", "unknown_principal"),
        (
            [entry("{u}", "read", "group")],
            "body.grants.0.principal_id",
            "unknown_principal",
        ),  # a user id as group
        ([entry("{deleted_group}", "read", "group")], "body.grants.0.principal_id", "unknown_principal"),
    ],
)
async def test_invalid_access_lists_change_nothing(
    env: Env, grants: list[dict[str, str]], field: str, code: str
) -> None:
    _, user_id = await signed_in(env, "user@example.com", [])
    deleted_group = await group(env, "Gone", [])
    assert (await env.admin.delete(f"/api/v1/groups/{deleted_group}")).status_code == 204
    resolved = [
        {**g, "principal_id": g["principal_id"].format(u=user_id, deleted_group=deleted_group)}
        for g in grants
    ]
    body = problem(await put_access(env, resolved), 422, "validation_error")
    assert (body["errors"][0]["field"], body["errors"][0]["code"]) == (field, code)
    assert await env.harness.sql("SELECT count(*) FROM knowledge_base_access") == [(0,)]


@pytest.mark.parametrize(
    "body",
    [
        {"grants": [{"principal_type": "role", "principal_id": MISSING, "access_level": "read"}]},
        {"grants": [{"principal_type": "user", "principal_id": MISSING, "access_level": "owner"}]},
        {"grants": [{"principal_type": "user", "principal_id": "x", "access_level": "read"}]},
        {"grants": [entry(MISSING, "read")] * 501},
        {"items": []},
    ],
)
async def test_malformed_access_bodies(env: Env, body: dict[str, Any]) -> None:
    response = await env.admin.put(f"{KBS}/{env.kb_id}/access", json=body)
    problem(response, 422, "validation_error")


async def test_a_manager_cannot_remove_their_own_manage(env: Env) -> None:
    manager, manager_id = await signed_in(env, "manager@example.com", [])
    _, other_id = await signed_in(env, "other@example.com", [])
    await grant(env, manager_id, "manage")
    for grants in ([], [entry(manager_id, "read")], [entry(other_id, "manage")]):
        body = problem(await put_access(env, grants, manager), 422, "validation_error")
        assert body["errors"][0]["code"] == "would_lose_manage"
    assert await env.harness.sql("SELECT user_id::text, access_level FROM knowledge_base_access") == [
        (manager_id, "manage")
    ]
    # Keeping manage through a group is enough.
    team = await group(env, "Team", [manager_id])
    response = await put_access(env, [entry(team, "manage", "group"), entry(other_id, "read")], manager)
    assert response.status_code == 200, response.text
    assert (await kb(env, manager)).json()["my_access_level"] == "manage"
    # knowledge_bases.manage needs no grant at all.
    assert (await put_access(env, [])).status_code == 200


async def test_access_list_needs_manage(env: Env) -> None:
    reader, reader_id = await signed_in(env, "reader@example.com", [])
    outsider, _ = await signed_in(env, "outsider@example.com", [])
    await grant(env, reader_id, "read")
    problem(await reader.get(f"{KBS}/{env.kb_id}/access"), 403, "permission_denied")
    problem(await put_access(env, [entry(reader_id, "manage")], reader), 403, "permission_denied")
    problem(await outsider.get(f"{KBS}/{env.kb_id}/access"), 404, "not_found")
    problem(await put_access(env, [], outsider), 404, "not_found")
    problem(await env.admin.get(f"{KBS}/{MISSING}/access"), 404, "not_found")


async def test_a_deleted_user_drops_out_of_the_access_list(env: Env) -> None:
    _, user_id = await signed_in(env, "leaver@example.com", [])
    await put_access(env, [entry(user_id, "read")])
    await env.harness.sql("UPDATE users SET deleted_at = now() WHERE id = :id", id=user_id)
    assert (await env.admin.get(f"{KBS}/{env.kb_id}/access")).json()["items"] == []


# --- reindex ----------------------------------------------------------------------------------------------


async def test_reindex_queues_every_document_without_a_running_job(env: Env) -> None:
    _, done = await indexed(env)
    busy_job, _ = await uploaded(env, "queued.md", b"# Queued\n\nText.")
    response = await env.admin.post(f"{KBS}/{env.kb_id}/reindex")
    assert response.status_code == 202, response.text
    body = response.json()
    assert (body["jobs_created"], body["already_queued"], body["without_version"]) == (1, 1, 0)
    [(kind, status, document_id)] = await env.harness.sql(
        "SELECT kind, status, document_id::text FROM ingestion_jobs WHERE id = :id", id=body["job_ids"][0]
    )
    assert (kind, status, document_id) == ("reindex", "queued", done)
    assert await job_row(env, busy_job) == ("queued", "fetch", None, 0, None)
    assert await audit_count(env, "kb.reindex.requested") == 1
    # The same call again finds both documents busy.
    again = (await env.admin.post(f"{KBS}/{env.kb_id}/reindex")).json()
    assert (again["jobs_created"], again["already_queued"]) == (0, 2)


async def test_reindex_needs_manage(env: Env) -> None:
    reader, reader_id = await signed_in(env, "reader@example.com", [])
    outsider, _ = await signed_in(env, "outsider@example.com", [])
    await grant(env, reader_id, "read")
    problem(await reader.post(f"{KBS}/{env.kb_id}/reindex"), 403, "permission_denied")
    problem(await outsider.post(f"{KBS}/{env.kb_id}/reindex"), 404, "not_found")
    empty = (await env.admin.post(f"{KBS}/{env.kb_id}/reindex")).json()
    assert (empty["jobs_created"], empty["already_queued"], empty["without_version"]) == (0, 0, 0)
