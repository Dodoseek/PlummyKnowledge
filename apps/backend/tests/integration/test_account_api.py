"""Activation links, own sessions and password change on real PostgreSQL and Redis (ADR-013, ADR-015)."""

from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

from tests.integration.harness import ADMIN_EMAIL, ADMIN_PASSWORD, Harness
from tests.integration.rbac import (
    CSRF_HEADER,
    OTHER_PASSWORD,
    activate,
    active_user,
    audit_actions,
    invite,
    login,
    setup_admin_client,
    token_from,
)

pytestmark = pytest.mark.integration

COOKIE = "__Host-pk_session"
INVITED = "invited@example.com"


@pytest.fixture
async def admin(harness: Harness) -> httpx.AsyncClient:
    return await setup_admin_client(harness)


async def _invited_token(admin: httpx.AsyncClient) -> str:
    return token_from((await invite(admin, INVITED))["activation"]["url"])


# --- activation -----------------------------------------------------------------------------------------


async def test_activation_sets_password_and_signs_in(
    harness: Harness, admin: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    token = await _invited_token(admin)
    caplog.set_level(logging.DEBUG)
    async with harness.new_client() as browser:
        response = await activate(browser, token)
        assert response.status_code == 200, response.text
        assert response.headers["cache-control"] == "no-store"
        assert COOKIE in response.headers["set-cookie"]
        body = response.json()
        assert body["user"]["email"] == INVITED
        assert (await browser.get("/api/v1/auth/me")).status_code == 200
    async with harness.new_client() as other:
        assert (await login(other, INVITED)).status_code == 200
    assert await harness.sql("SELECT status FROM users WHERE email = :e", e=INVITED) == [("active",)]
    assert await harness.sql("SELECT count(*) FROM user_activation_tokens") == [(0,)]
    assert "user.activated" in await audit_actions(harness)
    assert token not in caplog.text
    assert ADMIN_PASSWORD not in caplog.text


async def test_activation_link_is_single_use(harness: Harness, admin: httpx.AsyncClient) -> None:
    token = await _invited_token(admin)
    async with harness.new_client() as browser:
        assert (await activate(browser, token)).status_code == 200
        again = await activate(browser, token, password=OTHER_PASSWORD)
    assert again.status_code == 400
    assert again.json()["code"] == "activation_token_invalid"
    async with harness.new_client() as other:
        assert (await login(other, INVITED, OTHER_PASSWORD)).status_code == 401


async def test_invalid_tokens_get_the_same_error(harness: Harness, admin: httpx.AsyncClient) -> None:
    token = await _invited_token(admin)
    async with harness.new_client() as browser:
        malformed = await activate(browser, "short")
        unknown = await activate(browser, token[:-4] + ("AAAA" if not token.endswith("AAAA") else "BBBB"))
    assert malformed.status_code == unknown.status_code == 400
    assert malformed.json() == {**unknown.json(), "correlation_id": malformed.json().get("correlation_id")}
    rejected = await harness.sql(
        "SELECT details->>'reason' FROM audit_logs WHERE action = 'user.activation.rejected' ORDER BY id"
    )
    assert [row[0] for row in rejected] == ["malformed_token", "unknown_token"]


async def test_expired_token_is_rejected(harness: Harness, admin: httpx.AsyncClient) -> None:
    token = await _invited_token(admin)
    await harness.sql("UPDATE user_activation_tokens SET expires_at = now() - interval '1 second'")
    async with harness.new_client() as browser:
        response = await activate(browser, token)
    assert response.json()["code"] == "activation_token_invalid"
    assert await harness.sql("SELECT status FROM users WHERE email = :e", e=INVITED) == [("invited",)]


async def test_disabled_or_deleted_user_cannot_activate(harness: Harness, admin: httpx.AsyncClient) -> None:
    created = await invite(admin, INVITED)
    token = token_from(created["activation"]["url"])
    await admin.patch(f"/api/v1/users/{created['user']['id']}", json={"status": "disabled"})
    async with harness.new_client() as browser:
        disabled = await activate(browser, token)
    assert disabled.json()["code"] == "activation_token_invalid"
    second = await invite(admin, "second@example.com")
    await admin.delete(f"/api/v1/users/{second['user']['id']}")
    async with harness.new_client() as browser:
        deleted = await activate(browser, token_from(second["activation"]["url"]))
    assert deleted.json()["code"] == "activation_token_invalid"


async def test_weak_password_keeps_the_link_usable(harness: Harness, admin: httpx.AsyncClient) -> None:
    token = await _invited_token(admin)
    async with harness.new_client() as browser:
        weak = await activate(browser, token, password="short")
        assert weak.status_code == 422
        assert weak.json()["errors"][0]["field"] == "body.password"
        email = await activate(browser, token, password=INVITED)
        assert email.json()["errors"][0]["code"] == "password_matches_email"
        assert (await activate(browser, token)).status_code == 200


async def test_invited_user_cannot_log_in(harness: Harness, admin: httpx.AsyncClient) -> None:
    await invite(admin, INVITED)
    async with harness.new_client() as browser:
        response = await login(browser, INVITED, "")
        anything = await login(browser, INVITED, ADMIN_PASSWORD)
    assert response.status_code == 422
    assert anything.status_code == 401
    assert anything.json()["code"] == "invalid_credentials"


async def test_concurrent_activations_succeed_once(harness: Harness, admin: httpx.AsyncClient) -> None:
    token = await _invited_token(admin)
    async with harness.new_client() as first, harness.new_client() as second:
        results = await asyncio.gather(activate(first, token), activate(second, token, OTHER_PASSWORD))
    assert sorted(response.status_code for response in results) == [200, 400]
    assert await harness.sql(
        "SELECT count(*) FROM sessions s JOIN users u ON u.id = s.user_id WHERE u.email = :e", e=INVITED
    ) == [(1,)]


async def test_activation_is_rate_limited_per_ip(harness: Harness, admin: httpx.AsyncClient) -> None:
    async with harness.new_client() as browser:
        statuses = [(await activate(browser, "x" * 43)).status_code for _ in range(11)]
    assert statuses[:10] == [400] * 10
    assert statuses[10] == 429


async def test_activation_requires_trusted_origin(harness: Harness, admin: httpx.AsyncClient) -> None:
    token = await _invited_token(admin)
    async with harness.new_client(headers={"Origin": "https://evil.example"}) as browser:
        response = await activate(browser, token)
    assert response.status_code == 403
    assert await harness.sql("SELECT status FROM users WHERE email = :e", e=INVITED) == [("invited",)]


# --- own sessions ---------------------------------------------------------------------------------------


async def test_list_my_sessions_shows_only_own_active_sessions(
    harness: Harness, admin: httpx.AsyncClient
) -> None:
    await active_user(harness, admin, "member@example.com", ["member"])
    async with harness.new_client(headers={"User-Agent": "second-browser"}) as second:
        await login(second, ADMIN_EMAIL)
        async with harness.new_client() as member:
            await login(member, "member@example.com")
            response = await admin.get("/api/v1/auth/sessions")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    items = response.json()["items"]
    assert len(items) == 2
    assert [item["current"] for item in items].count(True) == 1
    assert {item["user_agent"] for item in items} >= {"second-browser"}
    assert all("token" not in key for item in items for key in item)


async def test_revoke_other_own_session(harness: Harness, admin: httpx.AsyncClient) -> None:
    async with harness.new_client() as second:
        await login(second, ADMIN_EMAIL)
        items = (await admin.get("/api/v1/auth/sessions")).json()["items"]
        other = next(item for item in items if not item["current"])
        response = await admin.delete(f"/api/v1/auth/sessions/{other['id']}")
        assert response.status_code == 204
        assert (await second.get("/api/v1/auth/me")).json()["code"] == "session_revoked"
        assert (await admin.get("/api/v1/auth/me")).status_code == 200
        assert (await admin.delete(f"/api/v1/auth/sessions/{other['id']}")).status_code == 404
    assert "auth.session.revoked" in await audit_actions(harness)


async def test_cannot_revoke_another_users_session(harness: Harness, admin: httpx.AsyncClient) -> None:
    await active_user(harness, admin, "member@example.com", ["member"])
    async with harness.new_client() as member:
        await login(member, "member@example.com")
        member_sessions = (await member.get("/api/v1/auth/sessions")).json()["items"]
        admin_session = (await admin.get("/api/v1/auth/sessions")).json()["items"][0]
        attack = await member.delete(f"/api/v1/auth/sessions/{admin_session['id']}")
        missing = await member.delete("/api/v1/auth/sessions/0190f3c4-0000-7000-8000-000000000000")
        assert attack.status_code == missing.status_code == 404
        assert attack.json()["code"] == missing.json()["code"]
        assert (await admin.get("/api/v1/auth/me")).status_code == 200
        assert admin_session["id"] not in {item["id"] for item in member_sessions}


async def test_revoke_current_session_clears_cookie(admin: httpx.AsyncClient) -> None:
    current = next(i for i in (await admin.get("/api/v1/auth/sessions")).json()["items"] if i["current"])
    response = await admin.delete(f"/api/v1/auth/sessions/{current['id']}")
    assert response.status_code == 204
    assert COOKIE in response.headers.get("set-cookie", "")
    assert (await admin.get("/api/v1/auth/me")).status_code == 401


async def test_revoke_session_requires_csrf(harness: Harness, admin: httpx.AsyncClient) -> None:
    current = (await admin.get("/api/v1/auth/sessions")).json()["items"][0]
    csrf = admin.headers.pop(CSRF_HEADER)
    response = await admin.delete(f"/api/v1/auth/sessions/{current['id']}")
    admin.headers[CSRF_HEADER] = csrf
    assert response.status_code == 403
    assert (await admin.get("/api/v1/auth/me")).status_code == 200


async def test_sessions_require_authentication(harness: Harness, admin: httpx.AsyncClient) -> None:
    async with harness.new_client() as anonymous:
        assert (await anonymous.get("/api/v1/auth/sessions")).status_code == 401


# --- password change ------------------------------------------------------------------------------------


async def _change(
    client: httpx.AsyncClient, current: str = ADMIN_PASSWORD, new: str = OTHER_PASSWORD
) -> httpx.Response:
    return await client.post(
        "/api/v1/auth/password/change", json={"current_password": current, "new_password": new}
    )


async def test_password_change_revokes_sessions_and_reissues_current(
    harness: Harness, admin: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    async with harness.new_client() as second:
        await login(second, ADMIN_EMAIL)
        response = await _change(admin)
        assert response.status_code == 200, response.text
        assert COOKIE in response.headers["set-cookie"]
        admin.headers[CSRF_HEADER] = response.json()["csrf_token"]
        assert (await admin.get("/api/v1/auth/me")).status_code == 200
        assert (await admin.get("/api/v1/auth/sessions")).json()["items"][0]["current"] is True
        assert (await second.get("/api/v1/auth/me")).status_code == 401
    async with harness.new_client() as browser:
        assert (await login(browser, ADMIN_EMAIL)).status_code == 401
        assert (await login(browser, ADMIN_EMAIL, OTHER_PASSWORD)).status_code == 200
    assert "auth.password.changed" in await audit_actions(harness)
    assert OTHER_PASSWORD not in caplog.text
    assert ADMIN_PASSWORD not in caplog.text


async def test_password_change_with_wrong_current_password(
    harness: Harness, admin: httpx.AsyncClient
) -> None:
    response = await _change(admin, current="wrong password entirely")
    assert response.status_code == 422
    assert response.json()["errors"][0]["code"] == "invalid_current_password"
    assert (await admin.get("/api/v1/auth/me")).status_code == 200
    denied = await harness.sql("SELECT outcome FROM audit_logs WHERE action = 'auth.password.changed'")
    assert denied == [("denied",)]


async def test_password_change_rejects_same_or_weak_password(admin: httpx.AsyncClient) -> None:
    same = await _change(admin, new=ADMIN_PASSWORD)
    weak = await _change(admin, new="short")
    assert same.json()["errors"][0]["code"] == "password_unchanged"
    assert weak.json()["errors"][0]["field"] == "body.new_password"


async def test_password_change_requires_csrf(admin: httpx.AsyncClient) -> None:
    csrf = admin.headers.pop(CSRF_HEADER)
    response = await _change(admin)
    admin.headers[CSRF_HEADER] = csrf
    assert response.status_code == 403


async def test_password_change_guessing_is_throttled(admin: httpx.AsyncClient) -> None:
    statuses = [(await _change(admin, current=f"wrong guess number {n}")).status_code for n in range(6)]
    assert statuses[:5] == [422] * 5
    assert statuses[5] == 429
