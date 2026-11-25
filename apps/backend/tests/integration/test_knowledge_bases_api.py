"""``/api/v1/knowledge-bases`` on real PostgreSQL and Redis (API_CONTRACTS.md §2.10, SECURITY_MODEL.md §3.5).

Grants are written directly to ``knowledge_base_access`` here; the access API is tested in
``test_knowledge_bases_manage_api``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.integration.harness import Harness, running_auth_app
from tests.integration.rbac import active_user, audit_actions, login, setup_admin_client

pytestmark = pytest.mark.integration

FAKE_KEY = "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789WXYZ"  # fake test value; secret-scan: allow
URL = "/api/v1/knowledge-bases"


@dataclass
class Env:
    harness: Harness
    admin: httpx.AsyncClient
    profile_id: str


@pytest.fixture
async def env(
    migrated_database_url: str, redis_url: str, clean_rate_limits: None, tmp_path: Path
) -> AsyncIterator[Env]:
    async with running_auth_app(migrated_database_url, redis_url, tmp_path / "bootstrap") as harness:
        admin = await setup_admin_client(harness)
        # A public IP literal passes the network policy without DNS; it is never contacted here.
        provider = await admin.post(
            "/api/v1/providers",
            json={
                "name": "OpenAI",
                "provider_type": "openai",
                "base_url": "https://1.1.1.1/v1",
                "api_key": FAKE_KEY,
            },
        )
        assert provider.status_code == 201, provider.text
        yield Env(harness, admin, await create_profile(admin, provider.json()["id"], "Small", 8))


async def create_profile(admin: httpx.AsyncClient, provider_id: str, name: str, dimensions: int) -> str:
    response = await admin.post(
        "/api/v1/embedding-profiles",
        json={
            "name": name,
            "provider_id": provider_id,
            "model_id": "text-embedding-3-small",
            "dimensions": dimensions,
        },
    )
    assert response.status_code == 201, response.text
    profile_id: str = response.json()["id"]
    return profile_id


async def create_kb(env: Env, name: str, **extra: Any) -> dict[str, Any]:
    response = await env.admin.post(URL, json={"name": name, "embedding_profile_id": env.profile_id, **extra})
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


def problem(response: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body: dict[str, Any] = response.json()
    assert body["code"] == code, body
    return body


async def signed_in(env: Env, email: str, roles: list[str]) -> tuple[httpx.AsyncClient, str]:
    user_id = await active_user(env.harness, env.admin, email, roles)
    client = env.harness.new_client()
    assert (await login(client, email)).status_code == 200
    return client, user_id


async def grant(
    env: Env, kb_id: str, level: str, *, user_id: str | None = None, group_id: str | None = None
) -> None:
    await env.harness.sql(
        "INSERT INTO knowledge_base_access "
        "(id, knowledge_base_id, principal_type, user_id, group_id, access_level) "
        "VALUES (gen_random_uuid(), :kb, :type, :user_id, :group_id, :level)",
        kb=kb_id,
        type="user" if user_id else "group",
        user_id=user_id,
        group_id=group_id,
        level=level,
    )


# --- create -------------------------------------------------------------------------------------------------


async def test_create_knowledge_base_with_its_upload_source(env: Env) -> None:
    created = await create_kb(env, "  Human   Resources ", description="  Policies and handbooks  ")
    assert created["name"] == "Human Resources"
    assert created["description"] == "Policies and handbooks"
    assert created["embedding_profile_id"] == env.profile_id
    assert created["my_access_level"] == "manage"
    assert created["indexing"] == {"documents_total": 0, "indexed": 0, "processing": 0, "failed": 0}

    sources = await env.harness.sql(
        "SELECT s.type, s.name, s.status, s.owner_knowledge_base_id::text, l.knowledge_base_id::text "
        "FROM data_sources s JOIN knowledge_base_sources l ON l.data_source_id = s.id"
    )
    assert sources == [("upload", "Uploads: Human Resources", "active", created["id"], created["id"])]
    assert "kb.created" in await audit_actions(env.harness)

    fetched = await env.admin.get(f"{URL}/{created['id']}")
    assert fetched.status_code == 200, fetched.text
    assert fetched.json() == created
    assert fetched.headers["cache-control"] == "no-store"
    listed = (await env.admin.get(URL)).json()
    assert [item["id"] for item in listed["items"]] == [created["id"]]


async def test_name_is_unique_among_live_knowledge_bases(env: Env) -> None:
    await create_kb(env, "Legal")
    for name in ("legal", " LEGAL "):
        problem(
            await env.admin.post(URL, json={"name": name, "embedding_profile_id": env.profile_id}),
            409,
            "knowledge_base_name_taken",
        )
    # a deleted knowledge base frees its name
    await env.harness.sql("UPDATE knowledge_bases SET deleted_at = now()")
    await create_kb(env, "Legal")


@pytest.mark.parametrize(
    ("body", "error_code"),
    [
        ({"name": "   "}, "invalid_name"),
        ({"name": "x" * 201}, "invalid_name"),
        ({"name": "bad\x00name"}, "invalid_name"),
        ({"name": "ok", "description": "d" * 2001}, "invalid_description"),
        ({"name": "ok", "description": "bell\x07"}, "invalid_description"),
    ],
)
async def test_invalid_names_and_descriptions(env: Env, body: dict[str, Any], error_code: str) -> None:
    body = {"embedding_profile_id": env.profile_id, **body}
    found = problem(await env.admin.post(URL, json=body), 422, "validation_error")
    assert found["errors"][0]["code"] == error_code
    assert await env.harness.sql("SELECT count(*) FROM knowledge_bases") == [(0,)]


async def test_request_shape_is_strict(env: Env) -> None:
    for body in (
        {"name": "A"},
        {"name": "A", "embedding_profile_id": "not-a-uuid"},
        {"name": "A", "embedding_profile_id": env.profile_id, "organization_id": env.profile_id},
    ):
        assert (await env.admin.post(URL, json=body)).status_code == 422


async def test_embedding_profile_must_be_usable(env: Env) -> None:
    async def refused(profile_id: str) -> str:
        found = problem(
            await env.admin.post(URL, json={"name": "KB", "embedding_profile_id": profile_id}),
            422,
            "validation_error",
        )
        error: dict[str, str] = found["errors"][0]
        assert error["field"].endswith("embedding_profile_id")
        return error["code"]

    assert await refused("00000000-0000-7000-8000-000000000000") == "unknown_embedding_profile"
    await env.harness.sql("UPDATE embedding_provider_configs SET status = 'deprecated'")
    assert await refused(env.profile_id) == "embedding_profile_not_active"
    # a profile from before ADR-017 can be larger than an HNSW index allows
    await env.harness.sql("UPDATE embedding_provider_configs SET status = 'active', dimensions = 4001")
    assert await refused(env.profile_id) == "dimensions_not_indexable"
    await env.harness.sql("UPDATE embedding_provider_configs SET dimensions = 8")
    await env.harness.sql("UPDATE ai_providers SET deleted_at = now()")
    assert await refused(env.profile_id) == "provider_unavailable"
    assert await env.harness.sql("SELECT count(*) FROM knowledge_bases") == [(0,)]
    assert await env.harness.sql("SELECT count(*) FROM data_sources") == [(0,)]


# --- access -------------------------------------------------------------------------------------------------


async def test_anonymous_requests_are_rejected(env: Env) -> None:
    kb = await create_kb(env, "Finance")
    async with env.harness.new_client() as anonymous:
        assert (await anonymous.get(URL)).status_code == 401
        assert (await anonymous.get(f"{URL}/{kb['id']}")).status_code == 401
        response = await anonymous.post(URL, json={"name": "X", "embedding_profile_id": env.profile_id})
        assert response.status_code in (401, 403)
    assert await env.harness.sql("SELECT count(*) FROM knowledge_bases") == [(1,)]


async def test_member_sees_only_granted_knowledge_bases(env: Env) -> None:
    hidden = await create_kb(env, "Board minutes")
    shared = await create_kb(env, "Handbook")
    member, member_id = await signed_in(env, "member@example.com", ["member"])
    async with aclosing(member):
        assert (await member.get(URL)).json()["items"] == []
        problem(await member.get(f"{URL}/{shared['id']}"), 404, "not_found")
        # an unknown id and a knowledge base without access answer the same
        unknown = await member.get(f"{URL}/00000000-0000-7000-8000-000000000000")
        no_access = await member.get(f"{URL}/{hidden['id']}")
        assert (unknown.status_code, unknown.json()["detail"]) == (
            no_access.status_code,
            no_access.json()["detail"],
        )
        problem(
            await member.post(URL, json={"name": "Mine", "embedding_profile_id": env.profile_id}),
            403,
            "permission_denied",
        )

        await grant(env, shared["id"], "read", user_id=member_id)
        listed = (await member.get(URL)).json()["items"]
        assert [(item["id"], item["my_access_level"]) for item in listed] == [(shared["id"], "read")]
        fetched = await member.get(f"{URL}/{shared['id']}")
        assert fetched.status_code == 200
        assert fetched.json()["my_access_level"] == "read"
        problem(await member.get(f"{URL}/{hidden['id']}"), 404, "not_found")

        # a grant on a deleted knowledge base gives nothing
        await env.harness.sql("UPDATE knowledge_bases SET deleted_at = now() WHERE id = :id", id=shared["id"])
        assert (await member.get(URL)).json()["items"] == []
        problem(await member.get(f"{URL}/{shared['id']}"), 404, "not_found")


async def test_group_grants_and_the_strongest_level(env: Env) -> None:
    kb = await create_kb(env, "Engineering")
    member, member_id = await signed_in(env, "dev@example.com", ["member"])
    group = await env.admin.post("/api/v1/groups", json={"name": "Developers"})
    assert group.status_code == 201, group.text
    group_id = group.json()["id"]
    members = await env.admin.put(f"/api/v1/groups/{group_id}/members", json={"user_ids": [member_id]})
    assert members.status_code == 200, members.text
    async with aclosing(member):
        await grant(env, kb["id"], "manage", group_id=group_id)
        await grant(env, kb["id"], "read", user_id=member_id)
        assert (await member.get(f"{URL}/{kb['id']}")).json()["my_access_level"] == "manage"
        assert [item["my_access_level"] for item in (await member.get(URL)).json()["items"]] == ["manage"]
        # manage on one knowledge base is not the knowledge_bases.manage permission
        problem(
            await member.post(URL, json={"name": "Other", "embedding_profile_id": env.profile_id}),
            403,
            "permission_denied",
        )

        # a deleted group's grants stop applying at once, even if membership rows remain
        await env.harness.sql("UPDATE groups SET deleted_at = now() WHERE id = :id", id=group_id)
        assert (await member.get(f"{URL}/{kb['id']}")).json()["my_access_level"] == "read"
        await env.harness.sql("DELETE FROM knowledge_base_access WHERE user_id = :id", id=member_id)
        assert (await member.get(URL)).json()["items"] == []
        problem(await member.get(f"{URL}/{kb['id']}"), 404, "not_found")


async def test_knowledge_manager_manages_every_knowledge_base(env: Env) -> None:
    first = await create_kb(env, "First")
    manager, _ = await signed_in(env, "km@example.com", ["knowledge_manager"])
    async with aclosing(manager):
        assert (await manager.get(f"{URL}/{first['id']}")).json()["my_access_level"] == "manage"
        created = await manager.post(URL, json={"name": "Second", "embedding_profile_id": env.profile_id})
        assert created.status_code == 201, created.text
        listed = (await manager.get(URL, params={"sort": "name"})).json()["items"]
        assert [(item["name"], item["my_access_level"]) for item in listed] == [
            ("First", "manage"),
            ("Second", "manage"),
        ]


# --- indexing and pagination --------------------------------------------------------------------------------


async def test_indexing_counts_live_documents_of_linked_sources(env: Env) -> None:
    kb = await create_kb(env, "Docs")
    other = await create_kb(env, "Other")
    statuses = ["indexed", "indexed", "pending", "processing", "failed", "skipped"]
    for index, status in enumerate(statuses):
        await env.harness.sql(
            "INSERT INTO documents (id, data_source_id, external_id, filename, processing_status) "
            "SELECT gen_random_uuid(), id, :ext, 'f.txt', :status FROM data_sources "
            "WHERE owner_knowledge_base_id = :kb",
            ext=f"doc-{index}",
            status=status,
            kb=kb["id"],
        )
    await env.harness.sql(
        "INSERT INTO documents (id, data_source_id, external_id, filename, processing_status, deleted_at, "
        "deletion_reason) SELECT gen_random_uuid(), id, 'gone', 'g.txt', 'indexed', now(), 'user' "
        "FROM data_sources WHERE owner_knowledge_base_id = :kb",
        kb=kb["id"],
    )
    fetched = (await env.admin.get(f"{URL}/{kb['id']}")).json()
    assert fetched["indexing"] == {"documents_total": 6, "indexed": 2, "processing": 2, "failed": 1}
    listed = {item["name"]: item["indexing"] for item in (await env.admin.get(URL)).json()["items"]}
    assert listed["Docs"] == fetched["indexing"]
    assert listed["Other"]["documents_total"] == 0
    assert other["indexing"]["documents_total"] == 0


async def test_cursor_pagination_over_visible_knowledge_bases(env: Env) -> None:
    names = ["Alpha", "Bravo", "Charlie", "Delta", "Echo"]
    ids = {name: (await create_kb(env, name))["id"] for name in names}
    member, member_id = await signed_in(env, "reader@example.com", ["member"])
    for name in ("Alpha", "Charlie", "Echo"):
        await grant(env, ids[name], "read", user_id=member_id)
    async with aclosing(member):
        seen: list[str] = []
        cursor: str | None = None
        while True:
            params: dict[str, Any] = {"sort": "-name", "limit": 2}
            if cursor:
                params["cursor"] = cursor
            page = (await member.get(URL, params=params)).json()
            seen += [item["name"] for item in page["items"]]
            cursor = page["next_cursor"]
            if not page["has_more"]:
                break
        assert seen == ["Echo", "Charlie", "Alpha"]
        assert (await member.get(URL, params={"sort": "size"})).status_code == 422
        assert (await member.get(URL, params={"knowledge_base_id": ids["Bravo"]})).status_code == 422
