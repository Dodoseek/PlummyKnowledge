"""User management API on real PostgreSQL and Redis (API_CONTRACTS.md §2.4, ADR-014, ADR-015)."""

from __future__ import annotations

import asyncio
import base64
import json

import httpx
import pytest

from tests.integration.conftest import TEST_ORIGIN
from tests.integration.harness import ADMIN_EMAIL, ADMIN_PASSWORD, Harness
from tests.integration.rbac import (
    CSRF_HEADER,
    active_user,
    audit_actions,
    invite,
    login,
    setup_admin_client,
    token_from,
)

pytestmark = pytest.mark.integration


@pytest.fixture
async def admin(harness: Harness) -> httpx.AsyncClient:
    return await setup_admin_client(harness)


async def _admin_id(harness: Harness) -> str:
    rows = await harness.sql("SELECT id FROM users WHERE email = :email", email=ADMIN_EMAIL)
    return str(rows[0][0])


# --- create ---------------------------------------------------------------------------------------------


async def test_create_user_returns_invited_user_and_one_time_link(
    harness: Harness, admin: httpx.AsyncClient
) -> None:
    response = await admin.post(
        "/api/v1/users",
        json={"email": " New.User@Example.com ", "display_name": " New User ", "roles": ["member"]},
    )
    assert response.status_code == 201, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    user = body["user"]
    assert user["email"] == "new.user@example.com"
    assert user["display_name"] == "New User"
    assert user["status"] == "invited"
    assert user["roles"] == ["member"]
    assert user["groups"] == []
    assert "password" not in json.dumps(body).replace("password_", "")
    url = body["activation"]["url"]
    assert url.startswith(f"{TEST_ORIGIN}/activate#token=")
    token = token_from(url)
    # Only a hash is stored; the plain token is nowhere in the database.
    stored = await harness.sql("SELECT token_hash FROM user_activation_tokens")
    assert len(stored) == 1
    assert token.encode() not in bytes(stored[0][0])
    password_rows = await harness.sql(
        "SELECT password_hash FROM users WHERE id = CAST(:id AS uuid)", id=user["id"]
    )
    assert password_rows == [(None,)]
    assert "user.created" in await audit_actions(harness)


async def test_create_user_with_taken_email_is_a_conflict(admin: httpx.AsyncClient) -> None:
    await invite(admin, "taken@example.com")
    again = await admin.post("/api/v1/users", json={"email": "TAKEN@example.com", "display_name": "X"})
    assert again.status_code == 409
    assert again.json()["code"] == "email_taken"
    admin_email = await admin.post("/api/v1/users", json={"email": ADMIN_EMAIL, "display_name": "X"})
    assert admin_email.json()["code"] == "email_taken"


@pytest.mark.parametrize(
    ("body", "field", "code"),
    [
        ({"email": "not-an-email", "display_name": "X"}, "body.email", "invalid_email"),
        ({"email": "a@example.com", "display_name": "   "}, "body.display_name", "invalid_display_name"),
        (
            {"email": "a@example.com", "display_name": "X", "roles": ["superuser"]},
            "body.roles",
            "unknown_role",
        ),
    ],
)
async def test_create_user_validates_input(
    admin: httpx.AsyncClient, body: dict[str, object], field: str, code: str
) -> None:
    response = await admin.post("/api/v1/users", json=body)
    assert response.status_code == 422, response.text
    error = response.json()["errors"][0]
    assert (error["field"], error["code"]) == (field, code)


async def test_create_user_rejects_unknown_fields_and_password(admin: httpx.AsyncClient) -> None:
    response = await admin.post(
        "/api/v1/users", json={"email": "a@example.com", "display_name": "A", "password": ADMIN_PASSWORD}
    )
    assert response.status_code == 422


async def test_create_user_requires_csrf_token(harness: Harness, admin: httpx.AsyncClient) -> None:
    csrf = admin.headers.pop(CSRF_HEADER)
    response = await admin.post("/api/v1/users", json={"email": "a@example.com", "display_name": "A"})
    admin.headers[CSRF_HEADER] = csrf
    assert response.status_code == 403
    assert response.json()["code"] == "csrf_failed"
    assert await harness.sql("SELECT count(*) FROM users") == [(1,)]


async def test_members_cannot_manage_or_read_users(harness: Harness, admin: httpx.AsyncClient) -> None:
    await active_user(harness, admin, "member@example.com", ["member"])
    async with harness.new_client() as member:
        await login(member, "member@example.com")
        listing = await member.get("/api/v1/users")
        create = await member.post("/api/v1/users", json={"email": "x@example.com", "display_name": "X"})
        roles = await member.put(f"/api/v1/users/{await _admin_id(harness)}/roles", json={"roles": []})
    assert listing.status_code == create.status_code == roles.status_code == 403
    assert listing.json()["code"] == "permission_denied"


async def test_user_manager_without_roles_assign_cannot_grant_roles_on_create(
    harness: Harness, admin: httpx.AsyncClient
) -> None:
    """``users.manage`` alone does not allow granting roles (no privilege escalation via create)."""
    group = (await admin.post("/api/v1/groups", json={"name": "User managers"})).json()
    await harness.sql(
        "INSERT INTO roles (id, key, name, description, is_system) "
        "VALUES (gen_random_uuid(), 'user_manager', 'User manager', 'test', false)"
    )
    await harness.sql(
        "INSERT INTO role_permissions (role_id, permission_id) SELECT r.id, p.id FROM roles r, permissions p "
        "WHERE r.key = 'user_manager' AND p.key IN ('users.manage', 'users.read')"
    )
    manager_id = await active_user(harness, admin, "manager@example.com", [])
    await admin.put(f"/api/v1/groups/{group['id']}/members", json={"user_ids": [manager_id]})
    assert (
        await admin.put(f"/api/v1/groups/{group['id']}/roles", json={"roles": ["user_manager"]})
    ).status_code == 200
    async with harness.new_client() as manager:
        await login(manager, "manager@example.com")
        plain = await manager.post("/api/v1/users", json={"email": "plain@example.com", "display_name": "P"})
        escalate = await manager.post(
            "/api/v1/users", json={"email": "evil@example.com", "display_name": "E", "roles": ["admin"]}
        )
    assert plain.status_code == 201
    assert escalate.status_code == 403
    assert await harness.sql("SELECT count(*) FROM users WHERE email = 'evil@example.com'") == [(0,)]


# --- list -----------------------------------------------------------------------------------------------


async def test_list_users_paginates_without_gaps_or_duplicates(admin: httpx.AsyncClient) -> None:
    for index in range(5):
        await invite(admin, f"user{index}@example.com")
    seen: list[str] = []
    cursor = None
    for _ in range(10):
        params = {"limit": "2", **({"cursor": cursor} if cursor else {})}
        page = (await admin.get("/api/v1/users", params=params)).json()
        seen.extend(item["email"] for item in page["items"])
        assert page["has_more"] == (page["next_cursor"] is not None)
        cursor = page["next_cursor"]
        if cursor is None:
            break
    assert seen == sorted(seen)
    assert len(seen) == len(set(seen)) == 6


async def test_list_users_sorting_and_filters(harness: Harness, admin: httpx.AsyncClient) -> None:
    await invite(admin, "alice@example.com", display_name="Alice")
    await active_user(harness, admin, "bob@example.com", ["member"])
    descending = (await admin.get("/api/v1/users", params={"sort": "-email"})).json()["items"]
    assert [item["email"] for item in descending] == ["bob@example.com", "alice@example.com", ADMIN_EMAIL]
    invited = (await admin.get("/api/v1/users", params={"status": "invited"})).json()["items"]
    assert [item["email"] for item in invited] == ["alice@example.com"]
    search = (await admin.get("/api/v1/users", params={"q": "ALI"})).json()["items"]
    assert [item["email"] for item in search] == ["alice@example.com"]
    # LIKE wildcards are matched literally.
    assert (await admin.get("/api/v1/users", params={"q": "%"})).json()["items"] == []
    assert (await admin.get("/api/v1/users", params={"q": "_"})).json()["items"] == []
    by_role = (await admin.get("/api/v1/users", params={"sort": "created_at"})).json()["items"]
    assert by_role[0]["email"] == ADMIN_EMAIL


async def test_list_users_filters_by_group(admin: httpx.AsyncClient) -> None:
    first = await invite(admin, "in@example.com")
    await invite(admin, "out@example.com")
    group = (await admin.post("/api/v1/groups", json={"name": "Team"})).json()
    await admin.put(f"/api/v1/groups/{group['id']}/members", json={"user_ids": [first["user"]["id"]]})
    items = (await admin.get("/api/v1/users", params={"group_id": group["id"]})).json()["items"]
    assert [item["email"] for item in items] == ["in@example.com"]


@pytest.mark.parametrize(
    ("params", "field", "code"),
    [
        ({"role": "admin"}, "query.role", "unknown_query_parameter"),
        ({"sort": "password_hash"}, "query.sort", "invalid_sort"),
        ({"limit": "0"}, "query.limit", "invalid_limit"),
        ({"limit": "101"}, "query.limit", "invalid_limit"),
        ({"cursor": "not-a-cursor"}, "query.cursor", "invalid_cursor"),
    ],
)
async def test_list_users_rejects_bad_query(
    admin: httpx.AsyncClient, params: dict[str, str], field: str, code: str
) -> None:
    response = await admin.get("/api/v1/users", params=params)
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "invalid_query_parameter"
    assert (body["errors"][0]["field"], body["errors"][0]["code"]) == (field, code)


async def test_cursor_is_bound_to_its_sort(admin: httpx.AsyncClient) -> None:
    for index in range(3):
        await invite(admin, f"user{index}@example.com")
    cursor = (await admin.get("/api/v1/users", params={"limit": "1"})).json()["next_cursor"]
    other_sort = await admin.get("/api/v1/users", params={"cursor": cursor, "sort": "-email"})
    assert other_sort.json()["errors"][0]["code"] == "invalid_cursor"
    forged = base64.urlsafe_b64encode(b'{"v":1,"s":"email","k":[1,2]}').decode().rstrip("=")
    assert (await admin.get("/api/v1/users", params={"cursor": forged})).status_code == 422


async def test_repeated_query_parameter_is_rejected(admin: httpx.AsyncClient) -> None:
    response = await admin.get("/api/v1/users?limit=1&limit=2")
    assert response.json()["errors"][0]["code"] == "duplicate_query_parameter"


# --- get / update / delete ------------------------------------------------------------------------------


async def test_get_unknown_user_is_not_found(admin: httpx.AsyncClient) -> None:
    response = await admin.get("/api/v1/users/0190f3c4-0000-7000-8000-000000000000")
    assert response.status_code == 404
    assert (await admin.get("/api/v1/users/not-a-uuid")).status_code == 422


async def test_disable_revokes_sessions_and_enable_restores_sign_in(
    harness: Harness, admin: httpx.AsyncClient
) -> None:
    user_id = await active_user(harness, admin, "member@example.com", ["member"])
    async with harness.new_client() as member:
        await login(member, "member@example.com")
        disabled = await admin.patch(f"/api/v1/users/{user_id}", json={"status": "disabled"})
        assert disabled.status_code == 200
        assert disabled.json()["status"] == "disabled"
        me = await member.get("/api/v1/auth/me")
        assert me.status_code == 401
        assert me.json()["code"] == "session_revoked"
        assert (await login(member, "member@example.com")).status_code == 401
        enabled = await admin.patch(f"/api/v1/users/{user_id}", json={"status": "active"})
        assert enabled.json()["status"] == "active"
        assert (await login(member, "member@example.com")).status_code == 200
    actions = await audit_actions(harness)
    assert "user.disabled" in actions
    assert "user.updated" in actions


async def test_rename_user(admin: httpx.AsyncClient) -> None:
    user_id = (await invite(admin, "rename@example.com"))["user"]["id"]
    response = await admin.patch(f"/api/v1/users/{user_id}", json={"display_name": "Renamed"})
    assert response.json()["display_name"] == "Renamed"
    assert response.json()["status"] == "invited"


async def test_invited_user_cannot_be_activated_by_admin(admin: httpx.AsyncClient) -> None:
    user_id = (await invite(admin, "invited@example.com"))["user"]["id"]
    response = await admin.patch(f"/api/v1/users/{user_id}", json={"status": "active"})
    assert response.status_code == 409
    assert response.json()["code"] == "invalid_user_state"
    assert (await admin.patch(f"/api/v1/users/{user_id}", json={"status": "invited"})).status_code == 422


async def test_disabled_invited_user_returns_to_invited(admin: httpx.AsyncClient) -> None:
    user_id = (await invite(admin, "invited@example.com"))["user"]["id"]
    await admin.patch(f"/api/v1/users/{user_id}", json={"status": "disabled"})
    enabled = await admin.patch(f"/api/v1/users/{user_id}", json={"status": "active"})
    assert enabled.json()["status"] == "invited"


async def test_admin_cannot_disable_or_delete_self(harness: Harness, admin: httpx.AsyncClient) -> None:
    admin_id = await _admin_id(harness)
    disable = await admin.patch(f"/api/v1/users/{admin_id}", json={"status": "disabled"})
    delete = await admin.delete(f"/api/v1/users/{admin_id}")
    assert disable.status_code == delete.status_code == 409
    assert disable.json()["code"] == delete.json()["code"] == "cannot_modify_self"


async def test_delete_is_soft_revokes_sessions_and_frees_email(
    harness: Harness, admin: httpx.AsyncClient
) -> None:
    user_id = await active_user(harness, admin, "gone@example.com", ["member"])
    group = (await admin.post("/api/v1/groups", json={"name": "Team"})).json()
    await admin.put(f"/api/v1/groups/{group['id']}/members", json={"user_ids": [user_id]})
    async with harness.new_client() as member:
        await login(member, "gone@example.com")
        assert (await admin.delete(f"/api/v1/users/{user_id}")).status_code == 204
        assert (await member.get("/api/v1/auth/me")).status_code == 401
        assert (await login(member, "gone@example.com")).status_code == 401
    assert (await admin.get(f"/api/v1/users/{user_id}")).status_code == 404
    assert (await admin.delete(f"/api/v1/users/{user_id}")).status_code == 404
    rows = await harness.sql(
        "SELECT deleted_at IS NOT NULL FROM users WHERE id = CAST(:id AS uuid)", id=user_id
    )
    assert rows == [(True,)]
    assert (await admin.get(f"/api/v1/groups/{group['id']}")).json()["members"] == []
    emails = [item["email"] for item in (await admin.get("/api/v1/users")).json()["items"]]
    assert "gone@example.com" not in emails
    again = await admin.post("/api/v1/users", json={"email": "gone@example.com", "display_name": "Back"})
    assert again.status_code == 201


# --- roles and the last administrator ------------------------------------------------------------------


async def test_set_user_roles_replaces_direct_roles(harness: Harness, admin: httpx.AsyncClient) -> None:
    user_id = await active_user(harness, admin, "member@example.com", ["member"])
    response = await admin.put(
        f"/api/v1/users/{user_id}/roles", json={"roles": ["auditor", "knowledge_manager"]}
    )
    assert response.json()["roles"] == ["auditor", "knowledge_manager"]
    async with harness.new_client() as member:
        await login(member, "member@example.com")
        me = (await member.get("/api/v1/auth/me")).json()
    assert me["roles"] == ["auditor", "knowledge_manager"]
    assert "audit.read" in me["permissions"]
    actions = await audit_actions(harness)
    assert actions.count("role.assigned") == 2
    assert "role.revoked" in actions
    bad = await admin.put(f"/api/v1/users/{user_id}/roles", json={"roles": ["nope"]})
    assert bad.json()["errors"][0]["code"] == "unknown_role"


async def test_last_administrator_cannot_lose_admin_role(harness: Harness, admin: httpx.AsyncClient) -> None:
    admin_id = await _admin_id(harness)
    response = await admin.put(f"/api/v1/users/{admin_id}/roles", json={"roles": ["member"]})
    assert response.status_code == 409
    assert response.json()["code"] == "last_administrator"
    assert (await admin.get("/api/v1/auth/me")).json()["roles"] == ["admin"]


async def test_admin_role_can_move_when_another_admin_exists(
    harness: Harness, admin: httpx.AsyncClient
) -> None:
    admin_id = await _admin_id(harness)
    await active_user(harness, admin, "second@example.com", ["admin"])
    response = await admin.put(f"/api/v1/users/{admin_id}/roles", json={"roles": ["member"]})
    assert response.status_code == 200
    assert (await admin.get("/api/v1/users")).status_code == 403


async def test_disabled_admin_does_not_count_as_active(harness: Harness, admin: httpx.AsyncClient) -> None:
    admin_id = await _admin_id(harness)
    second = await active_user(harness, admin, "second@example.com", ["admin"])
    assert (await admin.patch(f"/api/v1/users/{second}", json={"status": "disabled"})).status_code == 200
    response = await admin.put(f"/api/v1/users/{admin_id}/roles", json={"roles": []})
    assert response.json()["code"] == "last_administrator"


async def test_concurrent_demotions_keep_one_administrator(
    harness: Harness, admin: httpx.AsyncClient
) -> None:
    """Two admins remove each other's admin role at the same time: the row lock lets only one win."""
    admin_id = await _admin_id(harness)
    second_id = await active_user(harness, admin, "second@example.com", ["admin"])
    async with harness.new_client() as second:
        await login(second, "second@example.com")
        results = await asyncio.gather(
            admin.put(f"/api/v1/users/{second_id}/roles", json={"roles": []}),
            second.put(f"/api/v1/users/{admin_id}/roles", json={"roles": []}),
        )
    codes = sorted(response.status_code for response in results)
    assert codes in ([200, 409], [200, 403]), [r.text for r in results]
    remaining = await harness.sql(
        "SELECT count(*) FROM user_roles ur JOIN roles r ON r.id = ur.role_id WHERE r.key = 'admin'"
    )
    assert remaining == [(1,)]


async def test_concurrent_deletes_keep_one_administrator(harness: Harness, admin: httpx.AsyncClient) -> None:
    admin_id = await _admin_id(harness)
    second_id = await active_user(harness, admin, "second@example.com", ["admin"])
    async with harness.new_client() as second:
        await login(second, "second@example.com")
        await asyncio.gather(
            admin.delete(f"/api/v1/users/{second_id}"),
            second.delete(f"/api/v1/users/{admin_id}"),
        )
    active_admins = await harness.sql(
        "SELECT count(*) FROM users u JOIN user_roles ur ON ur.user_id = u.id "
        "JOIN roles r ON r.id = ur.role_id "
        "WHERE r.key = 'admin' AND u.deleted_at IS NULL AND u.status = 'active'"
    )
    assert active_admins == [(1,)]


# --- sessions and activation links ----------------------------------------------------------------------


async def test_admin_revokes_all_sessions_of_a_user(harness: Harness, admin: httpx.AsyncClient) -> None:
    user_id = await active_user(harness, admin, "member@example.com", ["member"])
    async with harness.new_client() as first, harness.new_client() as second:
        await login(first, "member@example.com")
        await login(second, "member@example.com")
        response = await admin.post(f"/api/v1/users/{user_id}/sessions/revoke")
        assert response.status_code == 200
        # Two logins plus the session opened by activation.
        assert response.json() == {"revoked": 3}
        assert (await first.get("/api/v1/auth/me")).status_code == 401
        assert (await second.get("/api/v1/auth/me")).status_code == 401
    assert "auth.session.revoked_by_admin" in await audit_actions(harness)


async def test_reissue_activation_invalidates_previous_link(
    harness: Harness, admin: httpx.AsyncClient
) -> None:
    created = await invite(admin, "invited@example.com")
    old_token = token_from(created["activation"]["url"])
    reissued = await admin.post(f"/api/v1/users/{created['user']['id']}/activation")
    assert reissued.status_code == 200
    new_token = token_from(reissued.json()["url"])
    assert new_token != old_token
    async with harness.new_client() as browser:
        old = await browser.post(
            "/api/v1/auth/activate", json={"token": old_token, "password": ADMIN_PASSWORD}
        )
        new = await browser.post(
            "/api/v1/auth/activate", json={"token": new_token, "password": ADMIN_PASSWORD}
        )
    assert old.status_code == 400
    assert new.status_code == 200


async def test_reissue_activation_only_for_invited_users(harness: Harness, admin: httpx.AsyncClient) -> None:
    response = await admin.post(f"/api/v1/users/{await _admin_id(harness)}/activation")
    assert response.status_code == 409
    assert response.json()["code"] == "invalid_user_state"
