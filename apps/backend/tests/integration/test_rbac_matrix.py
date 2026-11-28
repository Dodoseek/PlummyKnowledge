"""Every route of the inventory, for every role, on real PostgreSQL and Redis (SECURITY_MODEL §3.6, 12.2).

Three matrices, driven by ``tests/route_inventory.py``:

- **Permission routes** by the system roles and a user without roles: the role's permissions decide, a refusal
  is ``403 permission_denied``. The same matrix runs a second time with the routes' permission dependencies
  switched off, so it is the use case that refuses: the route check is an early refusal, not the only one
  (SECURITY_MODEL §6.1).
- **Knowledge base routes** by the ways to reach a knowledge base (direct read, read through a group, direct
  manage, ``knowledge_bases.manage``, none): without access ``404 not_found`` (existence is not disclosed),
  with ``read`` where ``manage`` is needed ``403 permission_denied``.
- **Personal routes** by another user: someone else's conversation, session or run does not exist.

Assistants follow ADR-005 and have their own matrix in ``test_assistant_access.py``.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from fastapi.routing import iter_route_contexts

from plummy.api.security import CurrentContext, RequirePermission
from plummy.application.security import SecurityContext
from plummy.domain.identity import Permission
from plummy.domain.identity.permissions import SYSTEM_ROLE_PERMISSIONS, SystemRole
from tests.integration import test_assistants_api as assistants
from tests.integration.harness import running_auth_app
from tests.integration.rbac import CSRF_HEADER
from tests.integration.test_document_upload import FAKE_KEY, Env, make_env, signed_in, upload
from tests.integration.test_documents_api import grant, indexed
from tests.integration.test_indexing import FakeGateway
from tests.integration.test_retrieval import TextEmbedder
from tests.route_inventory import ROUTES, V1, Access, Rule

pytestmark = pytest.mark.integration

MISSING = "00000000-0000-7000-8000-000000000000"
DENIED = (401, 403)

# --- permission routes -------------------------------------------------------------------------------------

# Bodies that pass validation, so that with the route check switched off the request reaches the use case.
BODIES: dict[tuple[str, str], dict[str, Any]] = {
    ("POST", "/users"): {"email": "matrix-new@example.com", "display_name": "Matrix"},
    ("PATCH", "/users/{user_id}"): {"display_name": "Matrix"},
    ("PUT", "/users/{user_id}/roles"): {"roles": []},
    ("POST", "/groups"): {"name": "Matrix group"},
    ("PATCH", "/groups/{group_id}"): {"name": "Matrix"},
    ("PUT", "/groups/{group_id}/members"): {"user_ids": []},
    ("PUT", "/groups/{group_id}/roles"): {"roles": []},
    ("POST", "/providers"): {
        "name": "Matrix",
        "provider_type": "openai",
        "base_url": "https://1.1.1.1/v1",
        "api_key": FAKE_KEY,
    },
    ("PATCH", "/providers/{provider_id}"): {"name": "Matrix"},
    ("POST", "/providers/{provider_id}/models"): {"model_id": "matrix"},
    ("PATCH", "/providers/{provider_id}/models/{config_id}"): {"enabled": False},
    ("POST", "/embedding-profiles"): {
        "name": "Matrix",
        "provider_id": MISSING,
        "model_id": "m",
        "dimensions": 8,
    },
    ("PATCH", "/embedding-profiles/{profile_id}"): {"name": "Matrix"},
    ("POST", "/knowledge-bases"): {"name": "Matrix", "embedding_profile_id": MISSING},
    ("POST", "/data-sources"): {"type": "google_drive", "name": "Matrix", "knowledge_base_id": MISSING},
    ("PATCH", "/data-sources/{source_id}"): {"name": "Matrix"},
    ("POST", "/data-sources/{source_id}/sync"): {},
}

PERMISSION_ROUTES = sorted(key for key, rule in ROUTES.items() if rule.access is Access.PERMISSION)
ROLES: tuple[SystemRole | None, ...] = (*SystemRole, None)


def concrete(path: str, **values: str) -> str:
    """The path with placeholders filled: the given values, otherwise an id that does not exist."""

    def fill(match: re.Match[str]) -> str:
        name = match.group(1)
        return values.get(name, "ingestion" if name == "job_type" else MISSING)

    return V1 + re.sub(r"\{(\w+)\}", fill, path)


async def send(
    client: httpx.AsyncClient, method: str, path: str, body: dict[str, Any] | None = None, **values: str
) -> httpx.Response:
    kwargs: dict[str, Any] = {} if body is None and method in {"GET", "DELETE"} else {"json": body or {}}
    return await client.request(method, concrete(path, **values), **kwargs)


def route_permission_dependencies(app: FastAPI) -> Iterator[RequirePermission]:
    for context in iter_route_contexts(app.routes):
        dependant = getattr(context, "dependant", None)
        stack = list(dependant.dependencies) if dependant is not None else []
        while stack:
            dependency = stack.pop()
            if isinstance(dependency.call, RequirePermission):
                yield dependency.call
            stack.extend(dependency.dependencies)


@dataclass
class Roles:
    env: Env
    clients: dict[SystemRole | None, httpx.AsyncClient]


@pytest.fixture
async def env(
    migrated_database_url: str, redis_url: str, clean_rate_limits: None, tmp_path: Path
) -> AsyncIterator[Env]:
    async with running_auth_app(migrated_database_url, redis_url, tmp_path / "bootstrap") as harness:
        yield await make_env(harness)


@pytest.fixture
async def roles(env: Env) -> Roles:
    clients: dict[SystemRole | None, httpx.AsyncClient] = {SystemRole.ADMIN: env.admin}
    for role in ROLES:
        if role is not SystemRole.ADMIN:
            name = role.value if role is not None else "nobody"
            clients[role], _ = await signed_in(env, f"{name}@example.com", [role.value] if role else [])
    return Roles(env, clients)


def allowed(role: SystemRole | None, permission: Permission) -> bool:
    return role is not None and permission in SYSTEM_ROLE_PERMISSIONS[role]


async def run_permission_matrix(roles: Roles) -> list[str]:
    mismatches: list[str] = []
    for method, path in PERMISSION_ROUTES:
        permission = ROUTES[method, path].permission
        assert permission is not None
        for role, client in roles.clients.items():
            response = await send(client, method, path, BODIES.get((method, path)))
            who = role.value if role else "no roles"
            if allowed(role, permission):
                if response.status_code in DENIED or response.status_code >= 500:
                    mismatches.append(
                        f"{method} {path} as {who}: {response.status_code} {response.text[:200]}"
                    )
            elif response.status_code != 403 or response.json().get("code") != "permission_denied":
                mismatches.append(f"{method} {path} as {who}: {response.status_code} {response.text[:200]}")
    return mismatches


async def test_permission_routes_follow_the_role_matrix(roles: Roles) -> None:
    assert await run_permission_matrix(roles) == []


async def test_use_cases_refuse_without_the_route_check(roles: Roles) -> None:
    app = roles.env.harness.app
    dependencies = set(route_permission_dependencies(app))
    assert len(dependencies) >= len({ROUTES[key].permission for key in PERMISSION_ROUTES})
    passed: list[SecurityContext] = []

    async def without_check(context: CurrentContext) -> SecurityContext:
        passed.append(context)
        return context

    for dependency in dependencies:
        app.dependency_overrides[dependency] = without_check
    try:
        assert await run_permission_matrix(roles) == []
        # The override was in effect: every permission route went through it, for every role.
        assert len(passed) == len(PERMISSION_ROUTES) * len(roles.clients)
    finally:
        for dependency in dependencies:
            app.dependency_overrides.pop(dependency, None)


# --- knowledge base routes ---------------------------------------------------------------------------------

READERS = ("admin", "knowledge_manager", "manager", "reader", "group_reader")
MANAGERS = ("admin", "knowledge_manager", "manager")
KB_BODIES: dict[tuple[str, str], dict[str, Any]] = {
    ("PATCH", "/knowledge-bases/{kb_id}"): {"description": "Matrix"},
    ("POST", "/knowledge-bases/{kb_id}/search"): {"query": "policy"},
}


@dataclass
class Knowledge:
    env: Env
    clients: dict[str, httpx.AsyncClient]
    document_id: str
    version_id: str
    chunk_id: str


@pytest.fixture
async def knowledge(env: Env, monkeypatch: pytest.MonkeyPatch) -> Knowledge:
    retrieve = env.harness.resources.knowledge.search._retrieve
    monkeypatch.setattr(retrieve, "_gateway", FakeGateway(TextEmbedder()))
    _, document_id = await indexed(env)
    [(version_id, chunk_id)] = await env.harness.sql(
        "SELECT document_version_id, id FROM document_chunks WHERE document_id = :id "
        "ORDER BY chunk_index LIMIT 1",
        id=document_id,
    )
    clients = {"admin": env.admin}
    clients["knowledge_manager"], _ = await signed_in(env, "km@example.com", ["knowledge_manager"])
    clients["manager"], manager_id = await signed_in(env, "manager@example.com", ["member"])
    clients["reader"], reader_id = await signed_in(env, "reader@example.com", ["member"])
    clients["group_reader"], group_reader_id = await signed_in(env, "group-reader@example.com", ["member"])
    clients["outsider"], _ = await signed_in(env, "outsider@example.com", ["member"])
    clients["auditor"], _ = await signed_in(env, "auditor@example.com", ["auditor"])
    await grant(env, manager_id, "manage")
    await grant(env, reader_id, "read")
    created = await env.admin.post("/api/v1/groups", json={"name": "Readers"})
    assert created.status_code == 201, created.text
    group_id = created.json()["id"]
    members = await env.admin.put(f"/api/v1/groups/{group_id}/members", json={"user_ids": [group_reader_id]})
    assert members.status_code == 200, members.text
    await env.harness.sql(
        "INSERT INTO knowledge_base_access (id, knowledge_base_id, principal_type, group_id, access_level) "
        "VALUES (gen_random_uuid(), :kb, 'group', :group_id, 'read')",
        kb=env.kb_id,
        group_id=group_id,
    )
    return Knowledge(env, clients, document_id, str(version_id), str(chunk_id))


async def current_grants(knowledge: Knowledge) -> dict[str, Any]:
    response = await knowledge.env.admin.get(f"{V1}/knowledge-bases/{knowledge.env.kb_id}/access")
    assert response.status_code == 200, response.text
    keys = ("principal_type", "principal_id", "access_level")
    return {"grants": [{key: item[key] for key in keys} for item in response.json()["items"]]}


async def kb_request(
    knowledge: Knowledge, client: httpx.AsyncClient, method: str, path: str
) -> httpx.Response:
    env = knowledge.env
    ids = {
        "kb_id": env.kb_id,
        "document_id": knowledge.document_id,
        "version_id": knowledge.version_id,
        "chunk_id": knowledge.chunk_id,
    }
    if (method, path) == ("GET", "/documents"):
        return await client.get(f"{V1}/documents", params={"knowledge_base_id": env.kb_id})
    if (method, path) == ("POST", "/documents"):
        return await upload(client, env.kb_id, "matrix.md", b"# Matrix\n\nA document of the access matrix.")
    if (method, path) == ("PUT", "/knowledge-bases/{kb_id}/access"):
        return await send(client, method, path, await current_grants(knowledge), **ids)
    return await send(client, method, path, KB_BODIES.get((method, path)), **ids)


KB_ROUTES = sorted(
    key
    for key, rule in ROUTES.items()
    if rule.access is Access.KNOWLEDGE_BASE
    and rule.level != "listed"
    and key != ("DELETE", "/documents/{document_id}")
)


def expected(rule: Rule, who: str) -> tuple[int, str] | None:
    """``None`` when the request must get through, otherwise the refusal."""
    if who not in READERS:
        return 404, "not_found"
    if rule.level == "manage" and who not in MANAGERS:
        return 403, "permission_denied"
    return None


async def test_knowledge_base_routes_follow_the_access_level(knowledge: Knowledge) -> None:
    mismatches: list[str] = []
    for method, path in KB_ROUTES:
        rule = ROUTES[method, path]
        for who, client in knowledge.clients.items():
            response = await kb_request(knowledge, client, method, path)
            refusal = expected(rule, who)
            if refusal is None:
                if response.status_code in (*DENIED, 404) or response.status_code >= 500:
                    mismatches.append(
                        f"{method} {path} as {who}: {response.status_code} {response.text[:200]}"
                    )
            elif (response.status_code, response.json().get("code")) != refusal:
                mismatches.append(f"{method} {path} as {who}: {response.status_code} {response.text[:200]}")
    assert mismatches == []


async def test_the_knowledge_base_list_shows_exactly_the_accessible_ones(knowledge: Knowledge) -> None:
    for who, client in knowledge.clients.items():
        response = await client.get(f"{V1}/knowledge-bases")
        assert response.status_code == 200, (who, response.text)
        ids = {item["id"] for item in response.json()["items"]}
        assert ids == ({knowledge.env.kb_id} if who in READERS else set()), who


async def test_deleting_a_document_needs_manage(knowledge: Knowledge) -> None:
    path = "/documents/{document_id}"
    for who, client in knowledge.clients.items():
        refusal = expected(ROUTES["DELETE", path], who)
        if refusal is not None:
            response = await send(client, "DELETE", path, document_id=knowledge.document_id)
            assert (response.status_code, response.json()["code"]) == refusal, (who, response.text)
    for who in MANAGERS:
        uploaded = await upload(
            knowledge.env.admin, knowledge.env.kb_id, f"{who}.md", b"# Delete me\n\nSoon."
        )
        assert uploaded.status_code == 202, uploaded.text
        document_id = uploaded.json()["document"]["id"]
        response = await send(knowledge.clients[who], "DELETE", path, document_id=document_id)
        assert response.status_code not in (*DENIED, 404), (who, response.text)
        assert response.status_code < 500, (who, response.text)


# --- personal routes ---------------------------------------------------------------------------------------

OWN_ROUTES = sorted(
    key for key, rule in ROUTES.items() if rule.access is Access.OWN and "{conversation_id}" in key[1]
)


@pytest.fixture
async def chat_env(
    migrated_database_url: str, redis_url: str, clean_rate_limits: None, tmp_path: Path
) -> AsyncIterator[assistants.Env]:
    async with running_auth_app(migrated_database_url, redis_url, tmp_path / "bootstrap") as harness:
        yield await assistants.make_env(harness)


async def test_someone_elses_personal_data_does_not_exist(chat_env: assistants.Env) -> None:
    env = chat_env
    assistant = await assistants.create(env, name="HR", knowledge_base_ids=[env.hr])
    owner, owner_id = await assistants.signed_in(env, "owner@example.com", ["member"])
    other, other_id = await assistants.signed_in(env, "other@example.com", ["member"])
    grants = [
        {"principal_type": "user", "principal_id": uid, "access_level": "read"}
        for uid in (owner_id, other_id)
    ]
    assert (
        await env.admin.put(f"{V1}/knowledge-bases/{env.hr}/access", json={"grants": grants})
    ).status_code == 200
    created = await owner.post(f"{V1}/conversations", json={"assistant_id": assistant["id"]})
    assert created.status_code == 201, created.text
    conversation_id = created.json()["id"]
    bodies = {("PATCH", "/conversations/{conversation_id}"): {"title": "Mine"}}

    # Someone else's conversation is not found by any route, not even by the administrator: personal data.
    for intruder in (other, env.admin):
        for method, path in OWN_ROUTES:
            response = await send(
                intruder, method, path, bodies.get((method, path)), conversation_id=conversation_id
            )
            assert (response.status_code, response.json()["code"]) == (404, "not_found"), (method, path)
        listed = await intruder.get(f"{V1}/conversations")
        assert conversation_id not in {item["id"] for item in listed.json()["items"]}
        stream = await intruder.post(
            f"{V1}/chat/stream",
            json={"conversation_id": conversation_id, "content": "Hi", "client_message_id": MISSING},
        )
        assert (stream.status_code, stream.json()["code"]) == (404, "not_found"), stream.text
    # The owner still has it.
    assert (await owner.get(f"{V1}/conversations/{conversation_id}")).status_code == 200

    # Someone else's session is not found either; it stays valid.
    sessions = await owner.get(f"{V1}/auth/sessions")
    session_id = sessions.json()["items"][0]["id"]
    for intruder in (other, env.admin):
        response = await intruder.delete(f"{V1}/auth/sessions/{session_id}")
        assert (response.status_code, response.json()["code"]) == (404, "not_found"), response.text
    assert (await owner.get(f"{V1}/auth/me")).status_code == 200


async def test_personal_routes_need_chat_use(chat_env: assistants.Env) -> None:
    env = chat_env
    assistant = await assistants.create(env, name="HR", knowledge_base_ids=[env.hr])
    auditor, _ = await assistants.signed_in(env, "auditor@example.com", ["auditor"])
    assert (await auditor.get(f"{V1}/auth/me")).status_code == 200  # a session, but no chat
    refusals = [
        await auditor.get(f"{V1}/conversations"),
        await auditor.post(f"{V1}/conversations", json={"assistant_id": assistant["id"]}),
        await auditor.post(f"{V1}/chat/stream", json={"content": "Hi", "client_message_id": MISSING}),
    ]
    for response in refusals:
        assert (response.status_code, response.json()["code"]) == (403, "permission_denied"), response.text


async def test_a_signed_out_client_gets_nothing_but_public_routes(env: Env) -> None:
    async with env.harness.new_client() as anonymous:
        for (method, path), rule in sorted(ROUTES.items()):
            if rule.access is Access.PUBLIC:
                continue
            response = await send(anonymous, method, path, BODIES.get((method, path)))
            assert (response.status_code, response.json()["code"]) == (401, "unauthenticated"), (method, path)


async def test_every_changing_route_needs_the_csrf_token(roles: Roles) -> None:
    """A signed-in administrator without ``X-CSRF-Token`` changes nothing, on any route (ADR-013 §2)."""
    admin = roles.clients[SystemRole.ADMIN]
    token = admin.headers.pop(CSRF_HEADER)
    try:
        for (method, path), rule in sorted(ROUTES.items()):
            if rule.access is Access.PUBLIC or method == "GET":
                continue
            response = await send(admin, method, path, BODIES.get((method, path)))
            assert (response.status_code, response.json()["code"]) == (403, "csrf_failed"), (method, path)
    finally:
        admin.headers[CSRF_HEADER] = token
