"""``/api/v1/assistants`` on real PostgreSQL and Redis (API_CONTRACTS.md §2.12, DATABASE_SCHEMA.md §5.3).

Configuration, validation and audit. Who can see and use an assistant (ADR-005, SC-01) is covered by
``test_assistant_access``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.integration.harness import Harness, running_auth_app
from tests.integration.rbac import active_user, audit_actions, login, setup_admin_client

pytestmark = pytest.mark.integration

FAKE_KEY = "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789WXYZ"  # fake test value; secret-scan: allow
URL = "/api/v1/assistants"
KBS = "/api/v1/knowledge-bases"
MISSING = "00000000-0000-7000-8000-000000000000"


@dataclass
class Env:
    harness: Harness
    admin: httpx.AsyncClient
    provider_id: str
    model_id: str
    profile_id: str
    hr: str
    it: str


async def make_env(harness: Harness) -> Env:
    admin = await setup_admin_client(harness)
    # A public IP literal passes the network policy without DNS; it is never contacted here.
    provider = await admin.post(
        "/api/v1/providers",
        json={"name": "P", "provider_type": "openai", "base_url": "https://1.1.1.1/v1", "api_key": FAKE_KEY},
    )
    assert provider.status_code == 201, provider.text
    provider_id = provider.json()["id"]
    model = await admin.post(
        f"/api/v1/providers/{provider_id}/models", json={"model_id": "gpt-x", "max_output_tokens": 4096}
    )
    assert model.status_code == 201, model.text
    profile_id = await create_profile(admin, provider_id, "E")
    hr = await create_kb(admin, "HR", profile_id)
    it = await create_kb(admin, "IT", profile_id)
    return Env(harness, admin, provider_id, model.json()["id"], profile_id, hr, it)


async def create_profile(admin: httpx.AsyncClient, provider_id: str, name: str) -> str:
    profile = await admin.post(
        "/api/v1/embedding-profiles",
        json={"name": name, "provider_id": provider_id, "model_id": "m", "dimensions": 8},
    )
    assert profile.status_code == 201, profile.text
    profile_id: str = profile.json()["id"]
    return profile_id


async def create_kb(admin: httpx.AsyncClient, name: str, profile_id: str) -> str:
    kb = await admin.post(KBS, json={"name": name, "embedding_profile_id": profile_id})
    assert kb.status_code == 201, kb.text
    kb_id: str = kb.json()["id"]
    return kb_id


@pytest.fixture
async def env(
    migrated_database_url: str, redis_url: str, clean_rate_limits: None, tmp_path: Path
) -> AsyncIterator[Env]:
    async with running_auth_app(migrated_database_url, redis_url, tmp_path / "bootstrap") as harness:
        yield await make_env(harness)


def body(env: Env, **overrides: Any) -> dict[str, Any]:
    return {
        "name": "HR Assistant",
        "model_config_id": env.model_id,
        "embedding_profile_id": env.profile_id,
        "knowledge_base_ids": [env.hr],
        **overrides,
    }


async def create(env: Env, **overrides: Any) -> dict[str, Any]:
    response = await env.admin.post(URL, json=body(env, **overrides))
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


def problem(response: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    payload: dict[str, Any] = response.json()
    assert payload["code"] == code, payload
    return payload


def field_error(response: httpx.Response) -> tuple[str, str]:
    payload = problem(response, 422, "validation_error")
    error = payload["errors"][0]
    return error["field"], error["code"]


async def signed_in(env: Env, email: str, roles: list[str]) -> tuple[httpx.AsyncClient, str]:
    user_id = await active_user(env.harness, env.admin, email, roles)
    client = env.harness.new_client()
    assert (await login(client, email)).status_code == 200
    return client, user_id


# --- create, read, update, delete -------------------------------------------------------------------------


async def test_create_with_defaults(env: Env) -> None:
    created = await create(env, name="  HR   Assistant ", instructions="  Answer briefly.  ")
    assert created["name"] == "HR Assistant"
    assert created["instructions"] == "Answer briefly."
    assert created["knowledge_base_ids"] == [env.hr]
    assert created["retrieval"] == {
        "top_k": 8,
        "vector_top_k": 40,
        "keyword_top_k": 40,
        "rrf_k": 60,
        "vector_weight": 1.0,
        "keyword_weight": 0.1,
    }
    assert created["limits"] == {
        "max_context_tokens": 8000,
        "max_history_messages": 10,
        "max_output_tokens": 1024,
    }
    assert created["status"] == "active"
    assert created["availability"] == {"available": True, "reason": None}
    assert (await env.admin.get(f"{URL}/{created['id']}")).json() == created
    rows = await env.harness.sql("SELECT details::text FROM audit_logs WHERE action = 'assistant.created'")
    assert len(rows) == 1
    assert "Answer briefly" not in rows[0][0]  # instructions never reach the audit log


async def test_create_with_settings(env: Env) -> None:
    created = await create(
        env,
        description="For HR",
        knowledge_base_ids=[env.it, env.hr],
        retrieval={"top_k": 5, "keyword_weight": 0.5, "rrf_k": 20},
        limits={"max_context_tokens": 4000, "max_history_messages": 0, "max_output_tokens": 4096},
        status="disabled",
    )
    assert created["knowledge_base_ids"] == sorted([env.hr, env.it])
    assert (created["retrieval"]["top_k"], created["retrieval"]["keyword_weight"]) == (5, 0.5)
    assert created["retrieval"]["rrf_k"] == 20
    assert created["limits"] == {
        "max_context_tokens": 4000,
        "max_history_messages": 0,
        "max_output_tokens": 4096,
    }
    assert created["availability"] == {"available": False, "reason": "disabled"}


async def test_patch_changes_only_what_is_sent(env: Env) -> None:
    created = await create(env, description="d", instructions="Be kind.")
    url = f"{URL}/{created['id']}"
    patched = (await env.admin.patch(url, json={"retrieval": {"top_k": 3}, "description": None})).json()
    assert patched["retrieval"] == {**created["retrieval"], "top_k": 3}
    assert patched["description"] is None
    assert (patched["name"], patched["instructions"]) == (created["name"], "Be kind.")
    assert patched["updated_at"] > created["updated_at"]
    moved = (await env.admin.patch(url, json={"knowledge_base_ids": [env.it]})).json()
    assert moved["knowledge_base_ids"] == [env.it]
    assert await env.harness.sql(
        "SELECT knowledge_base_id::text FROM assistant_knowledge_bases WHERE assistant_id = :id",
        id=created["id"],
    ) == [(env.it,)]
    emptied = (await env.admin.patch(url, json={"knowledge_base_ids": []})).json()
    assert emptied["knowledge_base_ids"] == []
    assert emptied["availability"] == {"available": True, "reason": None}  # preview for assistants.manage
    rows = await env.harness.sql(
        "SELECT details->>'fields' FROM audit_logs WHERE action = 'assistant.updated'"
    )
    assert [row[0] for row in rows] == ["description,retrieval", "knowledge_base_ids", "knowledge_base_ids"]
    # Nothing changed: nothing recorded.
    assert (await env.admin.patch(url, json={"name": created["name"]})).status_code == 200
    assert (await audit_actions(env.harness)).count("assistant.updated") == 3


async def test_delete(env: Env) -> None:
    created = await create(env)
    url = f"{URL}/{created['id']}"
    assert (await env.admin.delete(url)).status_code == 204
    problem(await env.admin.get(url), 404, "not_found")
    problem(await env.admin.delete(url), 404, "not_found")
    problem(await env.admin.patch(url, json={"name": "X"}), 404, "not_found")
    assert (await env.admin.get(URL)).json()["items"] == []
    assert await env.harness.sql("SELECT count(*) FROM assistant_knowledge_bases") == [(0,)]
    assert "assistant.deleted" in await audit_actions(env.harness)
    assert (await env.admin.post(URL, json=body(env))).status_code == 201  # the name is free again
    # The knowledge base is untouched.
    assert (await env.admin.get(f"{KBS}/{env.hr}")).status_code == 200


async def test_list_sorted_and_paginated(env: Env) -> None:
    for name in ("Charlie", "alpha", "Bravo"):
        await create(env, name=name)
    first = (await env.admin.get(URL, params={"limit": 2})).json()
    assert [item["name"] for item in first["items"]] == ["alpha", "Bravo"]
    assert first["has_more"] is True
    rest = (await env.admin.get(URL, params={"limit": 2, "cursor": first["next_cursor"]})).json()
    assert [item["name"] for item in rest["items"]] == ["Charlie"]
    newest = (await env.admin.get(URL, params={"sort": "-created_at"})).json()
    assert [item["name"] for item in newest["items"]] == ["Bravo", "alpha", "Charlie"]
    problem(await env.admin.get(URL, params={"sort": "status"}), 422, "invalid_query_parameter")


# --- validation -------------------------------------------------------------------------------------------


async def test_name_must_be_unique_among_live_assistants(env: Env) -> None:
    created = await create(env)
    problem(await env.admin.post(URL, json=body(env, name="hr ASSISTANT")), 409, "assistant_name_taken")
    other = await create(env, name="Other")
    problem(
        await env.admin.patch(f"{URL}/{other['id']}", json={"name": "HR Assistant"}),
        409,
        "assistant_name_taken",
    )
    assert (await env.admin.patch(f"{URL}/{created['id']}", json={"name": "HR assistant"})).status_code == 200


@pytest.mark.parametrize(
    ("overrides", "field", "code"),
    [
        ({"name": "   "}, "body.name", "invalid_name"),
        ({"instructions": "a\x00b"}, "body.instructions", "invalid_instructions"),
        ({"instructions": "x" * 20_001}, "body.instructions", "invalid_instructions"),
        ({"model_config_id": MISSING}, "body.model_config_id", "unknown_model"),
        ({"embedding_profile_id": MISSING}, "body.embedding_profile_id", "unknown_embedding_profile"),
        ({"knowledge_base_ids": [MISSING]}, "body.knowledge_base_ids", "unknown_knowledge_base"),
        ({"knowledge_base_ids": ["{hr}", "{hr}"]}, "body.knowledge_base_ids", "duplicate_knowledge_base"),
        ({"retrieval": {"top_k": 0}}, "body.retrieval", "out_of_range"),
        ({"retrieval": {"top_k": 50, "vector_top_k": 10}}, "body.retrieval", "out_of_range"),
        ({"retrieval": {"vector_weight": 0, "keyword_weight": 0}}, "body.retrieval", "out_of_range"),
        ({"retrieval": {"rrf_k": 1001}}, "body.retrieval", "out_of_range"),
        ({"limits": {"max_context_tokens": 100}}, "body.limits.max_context_tokens", "out_of_range"),
        ({"limits": {"max_history_messages": 101}}, "body.limits.max_history_messages", "out_of_range"),
        ({"limits": {"max_output_tokens": 0}}, "body.limits.max_output_tokens", "out_of_range"),
        ({"limits": {"max_output_tokens": 5000}}, "body.limits.max_output_tokens", "exceeds_model_limit"),
    ],
)
async def test_invalid_assistants_are_rejected(
    env: Env, overrides: dict[str, Any], field: str, code: str
) -> None:
    if overrides.get("knowledge_base_ids"):
        overrides = {
            **overrides,
            "knowledge_base_ids": [k.format(hr=env.hr) for k in overrides["knowledge_base_ids"]],
        }
    assert field_error(await env.admin.post(URL, json=body(env, **overrides))) == (field, code)
    assert await env.harness.sql("SELECT count(*) FROM assistants") == [(0,)]


@pytest.mark.parametrize(
    "payload",
    [
        {"name": "A", "model_config_id": MISSING},  # embedding profile missing
        {"tenant": "x"},
        {"status": "archived"},
        {"retrieval": {"top_k": 5, "min_score": 0.3}},
        {"limits": {"max_tokens": 5}},
        {"retrieval": {"keyword_weight": "NaN"}},
    ],
)
async def test_malformed_bodies(env: Env, payload: dict[str, Any]) -> None:
    full = payload if "name" in payload else body(env, **payload)
    problem(await env.admin.post(URL, json=full), 422, "validation_error")


async def test_every_knowledge_base_must_use_the_assistant_profile(env: Env) -> None:
    other_profile = await create_profile(env.admin, env.provider_id, "Other")
    other_kb = await create_kb(env.admin, "Elsewhere", other_profile)
    response = await env.admin.post(URL, json=body(env, knowledge_base_ids=[env.hr, other_kb]))
    assert field_error(response) == ("body.knowledge_base_ids", "embedding_profile_mismatch")
    created = await create(env)
    # Changing the profile alone leaves the knowledge bases behind.
    patch = {"embedding_profile_id": other_profile}
    assert field_error(await env.admin.patch(f"{URL}/{created['id']}", json=patch)) == (
        "body.knowledge_base_ids",
        "embedding_profile_mismatch",
    )
    moved = await env.admin.patch(f"{URL}/{created['id']}", json={**patch, "knowledge_base_ids": [other_kb]})
    assert moved.status_code == 200, moved.text
    assert moved.json()["embedding_profile_id"] == other_profile


async def test_a_disabled_model_is_refused_but_does_not_block_other_edits(env: Env) -> None:
    created = await create(env)
    model_url = f"/api/v1/providers/{env.provider_id}/models/{env.model_id}"
    assert (await env.admin.patch(model_url, json={"enabled": False})).status_code == 200
    assert field_error(await env.admin.post(URL, json=body(env, name="New"))) == (
        "body.model_config_id",
        "model_disabled",
    )
    disabled = await env.admin.patch(f"{URL}/{created['id']}", json={"status": "disabled"})
    assert disabled.status_code == 200, disabled.text


async def test_a_retired_profile_is_refused(env: Env) -> None:
    retired = await create_profile(env.admin, env.provider_id, "Old")
    response = await env.admin.patch(f"/api/v1/embedding-profiles/{retired}", json={"status": "deprecated"})
    assert response.status_code == 200, response.text
    assert field_error(
        await env.admin.post(URL, json=body(env, embedding_profile_id=retired, knowledge_base_ids=[]))
    ) == ("body.embedding_profile_id", "embedding_profile_not_active")


async def test_a_model_or_provider_in_use_cannot_be_deleted(env: Env) -> None:
    created = await create(env)
    model_url = f"/api/v1/providers/{env.provider_id}/models/{env.model_id}"
    problem(await env.admin.delete(model_url), 409, "model_in_use")
    problem(await env.admin.delete(f"/api/v1/providers/{env.provider_id}"), 409, "provider_in_use")
    # Once the assistant is deleted, the model can go.
    assert (await env.admin.delete(f"{URL}/{created['id']}")).status_code == 204
    assert (await env.admin.delete(model_url)).status_code == 204
    assert await env.harness.sql("SELECT model_config_id IS NULL FROM assistants") == [(True,)]


async def test_a_deleted_knowledge_base_leaves_its_assistants(env: Env) -> None:
    created = await create(env, knowledge_base_ids=[env.hr, env.it])
    assert (await env.admin.delete(f"{KBS}/{env.it}")).status_code == 204
    fetched = (await env.admin.get(f"{URL}/{created['id']}")).json()
    assert fetched["knowledge_base_ids"] == [env.hr]
    assert await env.harness.sql("SELECT count(*) FROM assistant_knowledge_bases") == [(1,)]


# --- permissions ------------------------------------------------------------------------------------------


async def test_only_assistants_manage_configures(env: Env) -> None:
    created = await create(env)
    knowledge_manager, _ = await signed_in(env, "km@example.com", ["knowledge_manager"])
    member, _ = await signed_in(env, "member@example.com", ["member"])
    for client in (knowledge_manager, member):
        problem(await client.post(URL, json=body(env, name="Mine")), 403, "permission_denied")
        problem(await client.patch(f"{URL}/{created['id']}", json={"name": "X"}), 403, "permission_denied")
        problem(await client.delete(f"{URL}/{created['id']}"), 403, "permission_denied")
    assert (await env.admin.get(f"{URL}/{created['id']}")).json()["name"] == "HR Assistant"


async def test_instructions_are_shown_to_managers_only(env: Env) -> None:
    created = await create(env, instructions="Secret sauce.")
    member, member_id = await signed_in(env, "member@example.com", ["member"])
    response = await env.admin.put(
        f"{KBS}/{env.hr}/access",
        json={"grants": [{"principal_type": "user", "principal_id": member_id, "access_level": "read"}]},
    )
    assert response.status_code == 200, response.text
    seen = (await member.get(f"{URL}/{created['id']}")).json()
    assert seen["instructions"] is None
    assert [item["instructions"] for item in (await member.get(URL)).json()["items"]] == [None]
    assert (await env.admin.get(f"{URL}/{created['id']}")).json()["instructions"] == "Secret sauce."


async def test_a_manager_cannot_attach_a_knowledge_base_they_cannot_read(env: Env) -> None:
    # assistants.manage without knowledge_bases.manage: a custom role on a group gives just that permission.
    builder, builder_id = await signed_in(env, "builder@example.com", [])
    await env.harness.sql(
        "INSERT INTO roles (id, key, name, description) VALUES (gen_random_uuid(), 'builder', 'Builder', '')"
    )
    await env.harness.sql(
        "INSERT INTO role_permissions (role_id, permission_id) SELECT r.id, p.id FROM roles r, permissions p "
        "WHERE r.key = 'builder' AND p.key IN ('assistants.manage', 'chat.use')"
    )
    await env.harness.sql(
        "INSERT INTO user_roles (user_id, role_id) SELECT :user, id FROM roles WHERE key = 'builder'",
        user=builder_id,
    )
    builder = env.harness.new_client()
    assert (await login(builder, "builder@example.com")).status_code == 200
    response = await builder.post(URL, json=body(env, name="Builder's"))
    assert field_error(response) == ("body.knowledge_base_ids", "unknown_knowledge_base")
    await env.admin.put(
        f"{KBS}/{env.hr}/access",
        json={"grants": [{"principal_type": "user", "principal_id": builder_id, "access_level": "read"}]},
    )
    assert (await builder.post(URL, json=body(env, name="Builder's"))).status_code == 201
