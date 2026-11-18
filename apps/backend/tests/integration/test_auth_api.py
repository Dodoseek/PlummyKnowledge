"""Login, logout, current session, CSRF and brute-force limits on real PostgreSQL/Redis (ADR-013)."""

from __future__ import annotations

import logging

import httpx
import pytest

from tests.integration.harness import ADMIN_EMAIL, ADMIN_PASSWORD, Harness

pytestmark = pytest.mark.integration

COOKIE = "__Host-pk_session"


async def _login(
    client: httpx.AsyncClient, email: str = ADMIN_EMAIL, password: str = ADMIN_PASSWORD
) -> httpx.Response:
    return await client.post("/api/v1/auth/login", json={"email": email, "password": password})


@pytest.fixture
async def admin(harness: Harness) -> Harness:
    await harness.setup_admin()
    return harness


# --- login ----------------------------------------------------------------------------------------------


async def test_login_me_and_logout(admin: Harness) -> None:
    async with admin.new_client() as browser:
        login = await _login(browser, email="ADMIN@example.com ")
        assert login.status_code == 200
        body = login.json()
        assert body["user"]["email"] == ADMIN_EMAIL
        assert body["roles"] == ["admin"]
        assert login.headers["cache-control"] == "no-store"
        assert "httponly" in login.headers["set-cookie"].lower()

        me = await browser.get("/api/v1/auth/me")
        assert me.status_code == 200
        assert me.json()["csrf_token"] == body["csrf_token"]  # stable for the session
        assert me.json()["session"]["expires_at"] == body["session"]["expires_at"]

        logout = await browser.post("/api/v1/auth/logout", headers={"X-CSRF-Token": body["csrf_token"]})
        assert logout.status_code == 204
        assert COOKIE in logout.headers["set-cookie"]
        assert "max-age=0" in logout.headers["set-cookie"].lower()

        after = await browser.get("/api/v1/auth/me")
        assert after.status_code == 401
    revoked = await admin.sql("SELECT revoke_reason FROM sessions WHERE revoked_at IS NOT NULL")
    assert ("logout",) in revoked
    actions = {row[0] for row in await admin.sql("SELECT action FROM audit_logs")}
    assert {"auth.login.succeeded", "auth.logout"} <= actions


async def test_session_survives_new_client_with_same_cookie(admin: Harness) -> None:
    """A page reload: only the cookie is kept; the CSRF token comes back from /auth/me."""
    async with admin.new_client() as browser:
        await _login(browser)
        cookie = browser.cookies[COOKIE]
    async with admin.new_client(cookies={COOKIE: cookie}) as reloaded:
        me = await reloaded.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["email"] == ADMIN_EMAIL


@pytest.mark.parametrize(
    ("email", "password"),
    [
        (ADMIN_EMAIL, "wrong password value"),
        ("nobody@example.com", ADMIN_PASSWORD),
        ("not-an-email", ADMIN_PASSWORD),
    ],
)
async def test_failed_login_does_not_reveal_which_part_is_wrong(
    admin: Harness, email: str, password: str
) -> None:
    async with admin.new_client() as browser:
        response = await _login(browser, email=email, password=password)
    assert response.status_code == 401
    body = response.json()
    assert body["code"] == "invalid_credentials"
    assert set(body) == {"type", "title", "status", "code", "correlation_id", "detail"}
    assert body["detail"] == "Invalid email or password."
    assert "set-cookie" not in response.headers


async def test_unknown_and_wrong_password_responses_are_identical(admin: Harness) -> None:
    async with admin.new_client() as browser:
        unknown = (await _login(browser, email="nobody@example.com")).json()
        wrong = (await _login(browser, password="wrong password value")).json()
    unknown.pop("correlation_id")
    wrong.pop("correlation_id")
    assert unknown == wrong


async def test_disabled_and_deleted_users_cannot_login(admin: Harness) -> None:
    await admin.sql("UPDATE users SET status = 'disabled'")
    async with admin.new_client() as browser:
        disabled = await _login(browser)
    await admin.sql("UPDATE users SET status = 'active', deleted_at = now()")
    async with admin.new_client() as browser:
        deleted = await _login(browser)
    assert disabled.status_code == deleted.status_code == 401
    assert disabled.json()["code"] == deleted.json()["code"] == "invalid_credentials"


async def test_no_self_registration(admin: Harness) -> None:
    body = {"email": "new@example.com", "password": ADMIN_PASSWORD}
    async with admin.new_client() as anonymous:
        for path in ("/api/v1/auth/register", "/api/v1/auth/signup"):
            assert (await anonymous.post(path, json=body)).status_code in {404, 405}
        # Users are created only by an administrator.
        assert (await anonymous.post("/api/v1/users", json=body)).status_code == 401
    assert await admin.sql("SELECT count(*) FROM users") == [(1,)]


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"email": ADMIN_EMAIL},
        {"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "role": "admin"},
        {"email": "", "password": ADMIN_PASSWORD},
        {"email": ADMIN_EMAIL, "password": "x" * 5000},
    ],
)
async def test_malformed_login_is_rejected(admin: Harness, payload: dict[str, str]) -> None:
    response = await admin.client.post("/api/v1/auth/login", json=payload)
    assert response.status_code == 422


async def test_login_requires_trusted_origin(admin: Harness) -> None:
    async with admin.new_client(headers={"Origin": "https://evil.example"}) as browser:
        response = await _login(browser)
    assert response.status_code == 403
    assert response.json()["code"] == "csrf_failed"
    assert "set-cookie" not in response.headers


# --- session fixation and revocation --------------------------------------------------------------------


async def test_login_rotates_session_and_revokes_previous_cookie(admin: Harness) -> None:
    async with admin.new_client() as browser:
        await _login(browser)
        first = browser.cookies[COOKIE]
        await _login(browser)
        second = browser.cookies[COOKIE]
    assert first != second
    async with admin.new_client(cookies={COOKIE: first}) as stale:
        response = await stale.get("/api/v1/auth/me")
    assert response.status_code == 401
    assert response.json()["code"] == "session_revoked"
    assert await admin.sql("SELECT count(*) FROM sessions WHERE revoke_reason = 'superseded'") == [(1,)]


async def test_attacker_planted_cookie_is_never_adopted(admin: Harness) -> None:
    planted = "A" * 43
    async with admin.new_client(cookies={COOKIE: planted}) as browser:
        response = await _login(browser)
    issued = response.cookies[COOKIE]
    assert issued != planted


@pytest.mark.parametrize("cookie", [None, "garbage", "A" * 43, "A" * 500])
async def test_me_without_valid_session(admin: Harness, cookie: str | None) -> None:
    cookies = {COOKIE: cookie} if cookie is not None else None
    async with admin.new_client(cookies=cookies) as browser:
        response = await browser.get("/api/v1/auth/me")
    assert response.status_code == 401
    assert response.json()["code"] == "unauthenticated"
    assert COOKIE in response.headers["set-cookie"]  # the browser is told to drop it


async def test_expired_sessions_are_rejected(admin: Harness) -> None:
    async with admin.new_client() as browser:
        await _login(browser)
        await admin.sql(
            "UPDATE sessions SET expires_at = now() - interval '1 second' WHERE revoked_at IS NULL"
        )
        absolute = await browser.get("/api/v1/auth/me")
    async with admin.new_client() as browser:
        await _login(browser)
        await admin.sql(
            "UPDATE sessions SET last_seen_at = now() - interval '25 hours' WHERE revoked_at IS NULL "
            "AND expires_at > now()"
        )
        idle = await browser.get("/api/v1/auth/me")
    assert absolute.status_code == idle.status_code == 401
    assert absolute.json()["code"] == idle.json()["code"] == "unauthenticated"


async def test_disabling_user_invalidates_existing_sessions(admin: Harness) -> None:
    async with admin.new_client() as browser:
        await _login(browser)
        await admin.sql("UPDATE users SET status = 'disabled'")
        response = await browser.get("/api/v1/auth/me")
    assert response.status_code == 401


async def test_last_seen_is_updated_at_most_once_a_minute(admin: Harness) -> None:
    async with admin.new_client() as browser:
        await _login(browser)
        await admin.sql(
            "UPDATE sessions SET last_seen_at = now() - interval '2 minutes' WHERE revoked_at IS NULL"
        )
        stale = await admin.sql("SELECT max(last_seen_at) FROM sessions")
        await browser.get("/api/v1/auth/me")
        first = await admin.sql("SELECT max(last_seen_at) FROM sessions")
        await browser.get("/api/v1/auth/me")
        second = await admin.sql("SELECT max(last_seen_at) FROM sessions")
    assert first != stale  # touched once after the interval
    assert first == second  # not written again within the minute


# --- logout and CSRF --------------------------------------------------------------------------------------


async def test_logout_is_idempotent_without_session(admin: Harness) -> None:
    async with admin.new_client() as browser:
        first = await browser.post("/api/v1/auth/logout")
        second = await browser.post("/api/v1/auth/logout")
    assert first.status_code == second.status_code == 204


async def test_logout_with_session_requires_csrf_token(admin: Harness) -> None:
    async with admin.new_client() as browser:
        await _login(browser)
        missing = await browser.post("/api/v1/auth/logout")
        wrong = await browser.post("/api/v1/auth/logout", headers={"X-CSRF-Token": "x" * 43})
        still_valid = await browser.get("/api/v1/auth/me")
    assert missing.status_code == wrong.status_code == 403
    assert missing.json()["code"] == "csrf_failed"
    assert still_valid.status_code == 200


async def test_csrf_token_of_another_session_is_rejected(admin: Harness) -> None:
    async with admin.new_client() as alice, admin.new_client() as mallory:
        await _login(alice)
        mallory_token = (await _login(mallory)).json()["csrf_token"]
        response = await alice.post("/api/v1/auth/logout", headers={"X-CSRF-Token": mallory_token})
    assert response.status_code == 403


async def test_logout_from_foreign_origin_is_rejected(admin: Harness) -> None:
    async with admin.new_client() as browser:
        csrf = (await _login(browser)).json()["csrf_token"]
        response = await browser.post(
            "/api/v1/auth/logout", headers={"X-CSRF-Token": csrf, "Origin": "https://evil.example"}
        )
    assert response.status_code == 403


# --- brute force ----------------------------------------------------------------------------------------


async def test_login_attempts_are_rate_limited_per_ip_and_email(admin: Harness) -> None:
    async with admin.new_client() as browser:
        for _ in range(5):
            assert (await _login(browser, password="wrong password value")).status_code == 401
        limited = await _login(browser)  # even the correct password is refused now
    assert limited.status_code == 429
    assert limited.json()["code"] == "rate_limited"
    assert int(limited.headers["retry-after"]) > 0


async def test_failed_attempts_per_email_are_limited_across_ips(admin: Harness) -> None:
    from redis.asyncio import Redis

    # Simulate 10 failures from other addresses: the per-email counter is independent of the IP.
    client = Redis.from_url(admin.settings.redis_url.get_secret_value())
    import hashlib

    key = "plummy:rl:login:email_failures:15m:" + hashlib.sha256(ADMIN_EMAIL.encode()).hexdigest()
    await client.set(key, 10, ex=900)
    await client.aclose()
    async with admin.new_client() as browser:
        response = await _login(browser)
    assert response.status_code == 429


async def test_unknown_email_is_limited_like_a_known_one(admin: Harness) -> None:
    async with admin.new_client() as browser:
        for _ in range(5):
            await _login(browser, email="nobody@example.com")
        limited = await _login(browser, email="nobody@example.com")
    assert limited.status_code == 429


async def test_login_fails_closed_without_redis(admin: Harness) -> None:
    from pathlib import Path

    from tests.integration.harness import running_auth_app

    async with running_auth_app(
        admin.settings.database_url.get_secret_value(),
        "redis://127.0.0.1:1/0",
        Path(admin.settings.bootstrap_dir),
    ) as h:
        response = await _login(h.client)
    assert response.status_code == 503
    assert response.json()["code"] == "service_unavailable"


# --- secrets ----------------------------------------------------------------------------------------------


async def test_session_token_is_stored_only_as_hash(admin: Harness) -> None:
    async with admin.new_client() as browser:
        await _login(browser)
        token = browser.cookies[COOKIE]
    rows = await admin.sql("SELECT encode(token_hash, 'hex') FROM sessions")
    import hashlib

    assert (hashlib.sha256(token.encode()).hexdigest(),) in rows
    dump = await admin.sql("SELECT row_to_json(s)::text FROM sessions s")
    assert all(token not in row[0] for row in dump)


async def test_password_and_token_never_logged(admin: Harness, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    async with admin.new_client() as browser:
        await _login(browser, password="wrong password value")
        await _login(browser)
        token = browser.cookies[COOKIE]
        await browser.get("/api/v1/auth/me")
    logged = "\n".join(f"{record.getMessage()} {record.__dict__}" for record in caplog.records)
    for secret in (ADMIN_PASSWORD, "wrong password value", token):
        assert secret not in logged
    audit = await admin.sql("SELECT row_to_json(a)::text FROM audit_logs a")
    assert all(ADMIN_PASSWORD not in row[0] and ADMIN_EMAIL not in row[0] for row in audit)
