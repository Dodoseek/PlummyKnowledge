"""Groups, role catalogs and read-only Instance Settings on real PostgreSQL and Redis (§2.5, §2.6, §2.17)."""

from __future__ import annotations

import json

import httpx
import pytest

from tests.integration.harness import Harness
from tests.integration.rbac import active_user, audit_actions, invite, login, setup_admin_client

pytestmark = pytest.mark.integration

MEMBER = "member@example.com"


@pytest.fixture
async def admin(harness: Harness) -> httpx.AsyncClient:
    return await setup_admin_client(harness)


async def _group(admin: httpx.AsyncClient, name: str = "Engineering", **extra: object) -> dict[str, object]:
    response = await admin.post("/api/v1/groups", json={"name": name, **extra})
    assert response.status_code == 201, response.text
    body: dict[str, object] = response.json()
    return body


# --- CRUD -----------------------------------------------------------------------------------------------


async def test_create_get_update_delete_group(harness: Harness, admin: httpx.AsyncClient) -> None:
    created = await _group(admin, "  Engineering  ", description="Builds things")
    assert created["name"] == "Engineering"
    assert (created["members"], created["roles"], created["member_count"]) == ([], [], 0)
    group_id = created["id"]
    fetched = (await admin.get(f"/api/v1/groups/{group_id}")).json()
    assert fetched["description"] == "Builds things"
    renamed = (await admin.patch(f"/api/v1/groups/{group_id}", json={"name": "Platform"})).json()
    assert renamed["name"] == "Platform"
    assert renamed["description"] == "Builds things"
    cleared = (await admin.patch(f"/api/v1/groups/{group_id}", json={"description": None})).json()
    assert cleared["description"] is None
    assert (await admin.delete(f"/api/v1/groups/{group_id}")).status_code == 204
    assert (await admin.get(f"/api/v1/groups/{group_id}")).status_code == 404
    assert (await admin.delete(f"/api/v1/groups/{group_id}")).status_code == 404
    actions = await audit_actions(harness)
    assert {"group.created", "group.updated", "group.deleted"} <= set(actions)


async def test_group_names_are_unique_case_insensitively(admin: httpx.AsyncClient) -> None:
    first = await _group(admin, "Sales")
    duplicate = await admin.post("/api/v1/groups", json={"name": "SALES"})
    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == "group_name_taken"
    other = await _group(admin, "Support")
    rename = await admin.patch(f"/api/v1/groups/{other['id']}", json={"name": "sales"})
    assert rename.json()["code"] == "group_name_taken"
    await admin.delete(f"/api/v1/groups/{first['id']}")
    assert (await admin.post("/api/v1/groups", json={"name": "Sales"})).status_code == 201


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"name": "   "}, "body.name"),
        ({"name": "x" * 101}, "body.name"),
        ({"name": "Ok", "description": "d" * 501}, "body.description"),
        ({"name": "bad\u0000name"}, "body.name"),
    ],
)
async def test_group_input_is_validated(admin: httpx.AsyncClient, body: dict[str, str], field: str) -> None:
    response = await admin.post("/api/v1/groups", json=body)
    assert response.status_code == 422, response.text
    assert response.json()["errors"][0]["field"] == field


async def test_list_groups_with_counts_search_and_pagination(
    harness: Harness, admin: httpx.AsyncClient
) -> None:
    user_id = (await invite(admin, "u@example.com"))["user"]["id"]
    for name in ("Alpha", "Beta", "Gamma"):
        group = await _group(admin, name)
        if name == "Beta":
            await admin.put(f"/api/v1/groups/{group['id']}/members", json={"user_ids": [user_id]})
    first = (await admin.get("/api/v1/groups", params={"limit": "2"})).json()
    assert [item["name"] for item in first["items"]] == ["Alpha", "Beta"]
    assert first["items"][1]["member_count"] == 1
    second = (await admin.get("/api/v1/groups", params={"limit": "2", "cursor": first["next_cursor"]})).json()
    assert [item["name"] for item in second["items"]] == ["Gamma"]
    assert second["has_more"] is False
    found = (await admin.get("/api/v1/groups", params={"q": "amm"})).json()["items"]
    assert [item["name"] for item in found] == ["Gamma"]
    descending = (await admin.get("/api/v1/groups", params={"sort": "-name"})).json()["items"]
    assert [item["name"] for item in descending] == ["Gamma", "Beta", "Alpha"]
    unknown = await admin.get("/api/v1/groups", params={"status": "x"})
    assert unknown.json()["code"] == "invalid_query_parameter"


# --- members and roles ----------------------------------------------------------------------------------


async def test_group_roles_apply_to_members_on_next_request(
    harness: Harness, admin: httpx.AsyncClient
) -> None:
    user_id = await active_user(harness, admin, MEMBER, ["member"])
    group = await _group(admin)
    async with harness.new_client() as member:
        await login(member, MEMBER)
        assert (await member.get("/api/v1/groups")).status_code == 403
        await admin.put(f"/api/v1/groups/{group['id']}/members", json={"user_ids": [user_id]})
        roles = await admin.put(f"/api/v1/groups/{group['id']}/roles", json={"roles": ["knowledge_manager"]})
        assert roles.json()["roles"] == ["knowledge_manager"]
        me = (await member.get("/api/v1/auth/me")).json()
        assert "knowledge_manager" in me["roles"]
        assert (await member.get("/api/v1/groups")).status_code == 200
        # Removing the member takes the permissions away immediately.
        await admin.put(f"/api/v1/groups/{group['id']}/members", json={"user_ids": []})
        assert (await member.get("/api/v1/groups")).status_code == 403
        assert "knowledge_manager" not in (await member.get("/api/v1/auth/me")).json()["roles"]
    user = (await admin.get(f"/api/v1/users/{user_id}")).json()
    assert user["roles"] == ["member"]
    assert "group.members.changed" in await audit_actions(harness)


async def test_deleting_group_revokes_its_permissions(harness: Harness, admin: httpx.AsyncClient) -> None:
    user_id = await active_user(harness, admin, MEMBER, ["member"])
    group = await _group(admin)
    await admin.put(f"/api/v1/groups/{group['id']}/members", json={"user_ids": [user_id]})
    await admin.put(f"/api/v1/groups/{group['id']}/roles", json={"roles": ["auditor"]})
    async with harness.new_client() as member:
        await login(member, MEMBER)
        assert "audit.read" in (await member.get("/api/v1/auth/me")).json()["permissions"]
        await admin.delete(f"/api/v1/groups/{group['id']}")
        assert "audit.read" not in (await member.get("/api/v1/auth/me")).json()["permissions"]
    assert await harness.sql("SELECT count(*) FROM user_groups") == [(0,)]
    assert await harness.sql("SELECT count(*) FROM group_roles") == [(0,)]


async def test_admin_role_cannot_be_granted_through_a_group(
    harness: Harness, admin: httpx.AsyncClient
) -> None:
    group = await _group(admin)
    response = await admin.put(f"/api/v1/groups/{group['id']}/roles", json={"roles": ["admin", "member"]})
    assert response.status_code == 422
    assert response.json()["errors"][0]["code"] == "role_not_assignable_to_group"
    assert await harness.sql("SELECT count(*) FROM group_roles") == [(0,)]
    unknown = await admin.put(f"/api/v1/groups/{group['id']}/roles", json={"roles": ["ghost"]})
    assert unknown.json()["errors"][0]["code"] == "unknown_role"


async def test_members_must_exist_and_not_be_deleted(admin: httpx.AsyncClient) -> None:
    group = await _group(admin)
    gone = (await invite(admin, "gone@example.com"))["user"]["id"]
    await admin.delete(f"/api/v1/users/{gone}")
    for user_ids in (["0190f3c4-0000-7000-8000-000000000000"], [gone]):
        response = await admin.put(f"/api/v1/groups/{group['id']}/members", json={"user_ids": user_ids})
        assert response.status_code == 422
        assert response.json()["errors"][0]["code"] == "unknown_user"


async def test_set_members_on_unknown_group_is_not_found(admin: httpx.AsyncClient) -> None:
    response = await admin.put(
        "/api/v1/groups/0190f3c4-0000-7000-8000-000000000000/members", json={"user_ids": []}
    )
    assert response.status_code == 404


async def test_user_details_list_groups(harness: Harness, admin: httpx.AsyncClient) -> None:
    user_id = (await invite(admin, "u@example.com"))["user"]["id"]
    group = await _group(admin, "Readers")
    await admin.put(f"/api/v1/groups/{group['id']}/members", json={"user_ids": [user_id]})
    details = (await admin.get(f"/api/v1/users/{user_id}")).json()
    assert details["groups"] == [{"id": group["id"], "name": "Readers"}]


async def test_members_cannot_manage_groups(harness: Harness, admin: httpx.AsyncClient) -> None:
    group = await _group(admin)
    await active_user(harness, admin, MEMBER, ["member"])
    async with harness.new_client() as member:
        await login(member, MEMBER)
        responses = [
            await member.post("/api/v1/groups", json={"name": "Mine"}),
            await member.get(f"/api/v1/groups/{group['id']}"),
            await member.patch(f"/api/v1/groups/{group['id']}", json={"name": "Hijacked"}),
            await member.delete(f"/api/v1/groups/{group['id']}"),
            await member.put(f"/api/v1/groups/{group['id']}/roles", json={"roles": ["auditor"]}),
        ]
    assert [r.status_code for r in responses] == [403] * 5
    assert (await admin.get(f"/api/v1/groups/{group['id']}")).json()["name"] == "Engineering"


# --- roles and instance settings ------------------------------------------------------------------------


async def test_role_and_permission_catalogs(harness: Harness, admin: httpx.AsyncClient) -> None:
    roles = (await admin.get("/api/v1/roles")).json()["items"]
    by_key = {role["key"]: role for role in roles}
    assert {"admin", "knowledge_manager", "member", "auditor"} <= by_key.keys()
    assert by_key["admin"]["is_system"] is True
    assert "users.manage" in by_key["admin"]["permissions"]
    assert by_key["member"]["permissions"] == ["chat.use"]
    permissions = (await admin.get("/api/v1/roles/permissions")).json()["items"]
    assert "roles.assign" in {item["key"] for item in permissions}
    await active_user(harness, admin, MEMBER, ["member"])
    async with harness.new_client() as member:
        await login(member, MEMBER)
        assert (await member.get("/api/v1/roles")).status_code == 403
        assert (await member.get("/api/v1/roles/permissions")).status_code == 403


async def test_instance_settings_are_read_only_and_secret_free(
    harness: Harness, admin: httpx.AsyncClient
) -> None:
    response = await admin.get("/api/v1/system/settings")
    assert response.status_code == 200
    body = response.json()
    assert body["public_url"] == harness.settings.public_url
    assert body["setup_mode"] == "web"
    assert body["session"]["cookie_secure"] is True
    assert body["session"]["idle_ttl_seconds"] == int(harness.settings.session_idle_ttl.total_seconds())
    # Key ring metadata only (ADR-016 §5): neither the key material nor the file path.
    assert body["secrets"] == {"configured": True, "primary_key_id": "test-1", "keys": 1}
    keyring_file = harness.settings.secrets_keyring_file
    assert keyring_file is not None
    key_material = json.loads(keyring_file.read_text())["keys"]["test-1"]
    rendered = json.dumps(body)
    assert key_material not in rendered
    assert str(keyring_file) not in rendered
    text = json.dumps({k: v for k, v in body.items() if k != "secrets"}).lower()
    for secret in ("database", "redis", "secret", "password"):
        assert secret not in text
    assert (await admin.patch("/api/v1/system/settings", json={"setup_mode": "cli"})).status_code == 405
    await active_user(harness, admin, MEMBER, ["member"])
    async with harness.new_client() as member:
        await login(member, MEMBER)
        assert (await member.get("/api/v1/system/settings")).status_code == 403
