"""First-run setup against real PostgreSQL and Redis (ADR-006, SECURITY_MODEL.md §4.5, SC-03/SC-04)."""

from __future__ import annotations

import asyncio
import logging
import stat

import pytest

from tests.integration.harness import ADMIN_EMAIL, ADMIN_PASSWORD, Harness, running_auth_app

pytestmark = pytest.mark.integration


# --- status ---------------------------------------------------------------------------------------------


async def test_status_before_and_after_initialization(harness: Harness) -> None:
    before = await harness.client.get("/api/v1/setup/status")
    assert before.status_code == 200
    assert before.json() == {"status": "uninitialized", "web_setup_enabled": True}
    assert before.headers["cache-control"] == "no-store"

    await harness.setup_admin()

    after = await harness.client.get("/api/v1/setup/status")
    assert after.json() == {"status": "initialized", "web_setup_enabled": True}


# --- positive -------------------------------------------------------------------------------------------


async def test_initialize_creates_admin_session_and_spends_token(harness: Harness) -> None:
    token = await harness.issue_bootstrap_token()
    token_file = harness.settings.bootstrap_dir / "setup-token"
    assert stat.S_IMODE(token_file.stat().st_mode) == 0o600
    assert stat.S_IMODE(token_file.parent.stat().st_mode) == 0o700

    response = await harness.initialize(token, email="  Admin@Example.COM ")

    assert response.status_code == 201
    body = response.json()
    assert body["user"]["email"] == ADMIN_EMAIL
    assert body["roles"] == ["admin"]
    assert "settings.manage" in body["permissions"]
    assert body["csrf_token"]
    assert response.headers["cache-control"] == "no-store"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith("__Host-pk_session=")
    for attribute in ("HttpOnly", "Secure", "SameSite=lax", "Path=/", "Max-Age="):
        assert attribute.lower() in cookie.lower()
    assert "password" not in response.text
    assert token not in response.text

    # The new session works immediately.
    me = await harness.client.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert me.json()["user"]["email"] == ADMIN_EMAIL

    state = await harness.sql(
        "SELECT initialized_at, initialized_by_user_id, bootstrap_token_hash FROM instance_state"
    )
    assert state[0][0] is not None
    assert str(state[0][1]) == body["user"]["id"]
    assert state[0][2] is None  # the token hash is erased
    users = await harness.sql("SELECT email, password_hash FROM users")
    assert users[0][0] == ADMIN_EMAIL
    assert users[0][1].startswith("$argon2id$")
    actions = await harness.sql("SELECT action, outcome FROM audit_logs ORDER BY created_at")
    assert ("setup.completed", "success") in actions


# --- negative -------------------------------------------------------------------------------------------


async def test_repeated_initialization_is_rejected_with_any_token(harness: Harness) -> None:
    token = await harness.issue_bootstrap_token()
    assert (await harness.initialize(token)).status_code == 201

    async with harness.new_client() as other:
        same_token = await harness.initialize(token, client=other, email="second@example.com")
        wrong_token = await harness.initialize("x" * 43, client=other, email="second@example.com")

    assert same_token.status_code == 409
    assert same_token.json()["code"] == "setup_already_completed"
    assert wrong_token.status_code == 409
    assert wrong_token.json()["code"] == "setup_already_completed"
    assert await harness.sql("SELECT count(*) FROM users") == [(1,)]
    # SC-03: every attempt is recorded.
    rejected = await harness.sql("SELECT details FROM audit_logs WHERE action = 'setup.initialize.rejected'")
    assert rejected == [({"reason": "already_completed"},)] * 2


async def test_reissuing_token_after_initialization_does_not_reopen_setup(harness: Harness) -> None:
    await harness.setup_admin()
    services_token_file = harness.settings.bootstrap_dir / "setup-token"
    from plummy.composition import build_cli_services
    from tests.integration.harness import fast_hasher

    services = build_cli_services(harness.settings, password_hasher=fast_hasher())
    try:
        result = await services.issue_bootstrap_token.execute(rotate=True)
    finally:
        await services.aclose()
    assert result.outcome.value == "already_initialized"
    assert not services_token_file.exists()
    status = await harness.client.get("/api/v1/setup/status")
    assert status.json()["status"] == "initialized"


@pytest.mark.parametrize("token", ["", "short", "x" * 43, "y" * 257])
async def test_wrong_token_is_rejected(harness: Harness, token: str) -> None:
    await harness.issue_bootstrap_token()
    response = await harness.initialize(token)
    if token == "" or len(token) > 256:
        assert response.status_code == 422
    else:
        assert response.status_code == 403
        assert response.json()["code"] == "setup_token_invalid"
        rejected = await harness.sql(
            "SELECT details FROM audit_logs WHERE action = 'setup.initialize.rejected'"
        )
        assert rejected == [({"reason": "token_invalid"},)]
    assert await harness.sql("SELECT count(*) FROM users") == [(0,)]


async def test_no_token_issued_means_setup_is_closed(harness: Harness) -> None:
    response = await harness.initialize("x" * 43)
    assert response.status_code == 403
    assert response.json()["code"] == "setup_token_invalid"


async def test_expired_token_is_rejected(harness: Harness) -> None:
    token = await harness.issue_bootstrap_token()
    await harness.sql("UPDATE instance_state SET bootstrap_token_expires_at = now() - interval '1 second'")
    response = await harness.initialize(token)
    assert response.status_code == 403
    assert response.json()["code"] == "setup_token_invalid"


async def test_rotated_token_invalidates_previous_one(harness: Harness) -> None:
    old = await harness.issue_bootstrap_token()
    assert await harness.issue_bootstrap_token() == old  # idempotent without --rotate
    new = await harness.issue_bootstrap_token(rotate=True)
    assert new != old
    assert (await harness.initialize(old)).status_code == 403
    assert (await harness.initialize(new)).status_code == 201


async def test_lost_token_file_is_reissued(harness: Harness) -> None:
    old = await harness.issue_bootstrap_token()
    (harness.settings.bootstrap_dir / "setup-token").unlink()
    new = await harness.issue_bootstrap_token()
    assert new != old
    assert (await harness.initialize(old)).status_code == 403
    assert (await harness.initialize(new)).status_code == 201


@pytest.mark.parametrize(
    ("admin_patch", "field", "code"),
    [
        ({"password": "tiny-pass1"}, "body.admin.password", "password_too_short"),
        ({"password": "a" * 129}, "body.admin.password", "password_too_long"),
        ({"password": "aaaaaaaaaaaaaaaa"}, "body.admin.password", "password_repetitive"),
        (
            {"email": "longenough@example.com", "password": "LongEnough@Example.com"},
            "body.admin.password",
            "password_matches_email",
        ),
        ({"email": "not-an-email"}, "body.admin.email", "invalid_email"),
        ({"display_name": "   "}, "body.admin.display_name", "invalid_display_name"),
    ],
)
async def test_invalid_admin_fields_are_rejected(
    harness: Harness, admin_patch: dict[str, str], field: str, code: str
) -> None:
    token = await harness.issue_bootstrap_token()
    admin = {"email": ADMIN_EMAIL, "display_name": "Admin", "password": ADMIN_PASSWORD, **admin_patch}
    response = await harness.client.post(
        "/api/v1/setup/initialize", json={"bootstrap_token": token, "admin": admin}
    )
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert body["errors"][0]["field"] == field
    assert body["errors"][0]["code"] == code
    assert admin["password"] not in response.text
    # The token is still usable after a validation error.
    assert (await harness.initialize(token)).status_code == 201


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"bootstrap_token": "x"},
        {"bootstrap_token": "x", "admin": {"email": ADMIN_EMAIL, "display_name": "A"}},
        {
            "bootstrap_token": "x",
            "admin": {"email": ADMIN_EMAIL, "display_name": "A", "password": "p", "role": "admin"},
        },
        {
            "bootstrap_token": "x",
            "admin": {"email": ADMIN_EMAIL, "display_name": "A", "password": "p"},
            "permissions": ["settings.manage"],
        },
    ],
)
async def test_malformed_requests_are_rejected(harness: Harness, payload: dict[str, object]) -> None:
    response = await harness.client.post("/api/v1/setup/initialize", json=payload)
    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "https://evil.example"},
        {"Origin": "null"},
        {"Origin": "", "Referer": "https://evil.example/x"},
    ],
)
async def test_foreign_origin_is_rejected(harness: Harness, headers: dict[str, str]) -> None:
    token = await harness.issue_bootstrap_token()
    response = await harness.client.post(
        "/api/v1/setup/initialize",
        json={
            "bootstrap_token": token,
            "admin": {"email": ADMIN_EMAIL, "display_name": "A", "password": ADMIN_PASSWORD},
        },
        headers=headers,
    )
    assert response.status_code == 403
    assert response.json()["code"] == "csrf_failed"
    assert await harness.sql("SELECT count(*) FROM users") == [(0,)]


async def test_missing_origin_and_referer_is_rejected(harness: Harness) -> None:
    token = await harness.issue_bootstrap_token()
    async with harness.new_client() as client:
        del client.headers["Origin"]
        response = await harness.initialize(token, client=client)
    assert response.status_code == 403
    assert response.json()["code"] == "csrf_failed"


async def test_referer_from_trusted_origin_is_accepted(harness: Harness) -> None:
    token = await harness.issue_bootstrap_token()
    async with harness.new_client() as client:
        del client.headers["Origin"]
        client.headers["Referer"] = "https://testserver/setup"
        response = await harness.initialize(token, client=client)
    assert response.status_code == 201


async def test_rate_limit_applies_per_ip(harness: Harness) -> None:
    for _ in range(5):
        assert (await harness.initialize("x" * 43)).status_code == 403
    limited = await harness.initialize("x" * 43)
    assert limited.status_code == 429
    assert limited.json()["code"] == "rate_limited"
    assert int(limited.headers["retry-after"]) > 0


async def test_cli_only_mode_disables_web_initialization(
    migrated_database_url: str, redis_url: str, clean_rate_limits: None, tmp_path: object
) -> None:
    from pathlib import Path

    async with running_auth_app(
        migrated_database_url, redis_url, Path(str(tmp_path)) / "b", setup_mode="cli_only"
    ) as h:
        token = await h.issue_bootstrap_token()
        status = await h.client.get("/api/v1/setup/status")
        response = await h.initialize(token)
    assert status.json() == {"status": "uninitialized", "web_setup_enabled": False}
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


async def test_redis_unavailable_fails_closed(migrated_database_url: str, tmp_path: object) -> None:
    from pathlib import Path

    async with running_auth_app(
        migrated_database_url, "redis://127.0.0.1:1/0", Path(str(tmp_path)) / "b"
    ) as h:
        token = await h.issue_bootstrap_token()
        response = await h.initialize(token)
        users = await h.sql("SELECT count(*) FROM users")
    assert response.status_code == 503
    assert response.json()["code"] == "service_unavailable"
    assert users == [(0,)]


# --- concurrency and atomicity ----------------------------------------------------------------------------


async def test_parallel_initialization_creates_exactly_one_admin(harness: Harness) -> None:
    token = await harness.issue_bootstrap_token()
    clients = [harness.new_client() for _ in range(5)]
    try:
        responses = await asyncio.gather(
            *(
                harness.initialize(token, client=client, email=f"admin{index}@example.com")
                for index, client in enumerate(clients)
            )
        )
    finally:
        for client in clients:
            await client.aclose()
    statuses = sorted(response.status_code for response in responses)
    assert statuses == [201, 409, 409, 409, 409]
    assert await harness.sql("SELECT count(*) FROM users") == [(1,)]
    assert await harness.sql("SELECT count(*) FROM user_roles") == [(1,)]
    assert await harness.sql("SELECT count(*) FROM sessions") == [(1,)]


async def test_failure_mid_transaction_leaves_instance_uninitialized(harness: Harness) -> None:
    token = await harness.issue_bootstrap_token()
    # Make the last write of the initialization transaction fail inside PostgreSQL.
    await harness.sql(
        "CREATE FUNCTION fail_insert() RETURNS trigger LANGUAGE plpgsql AS "
        "$$ BEGIN RAISE EXCEPTION 'injected failure'; END $$"
    )
    await harness.sql(
        "CREATE TRIGGER fail_sessions BEFORE INSERT ON sessions FOR EACH ROW EXECUTE FUNCTION fail_insert()"
    )

    failed = await harness.initialize(token)

    assert failed.status_code == 500
    assert "injected" not in failed.text
    assert await harness.sql("SELECT count(*) FROM users") == [(0,)]
    assert await harness.sql("SELECT count(*) FROM user_roles") == [(0,)]
    state = await harness.sql("SELECT initialized_at, bootstrap_token_hash IS NOT NULL FROM instance_state")
    assert state == [(None, True)]

    await harness.sql("DROP TRIGGER fail_sessions ON sessions")
    assert (await harness.initialize(token)).status_code == 201  # the same token still works


# --- secrets never leak -----------------------------------------------------------------------------------


async def test_token_and_password_never_reach_logs(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    token = await harness.issue_bootstrap_token()
    await harness.initialize("z" * 43)
    await harness.initialize(token, password="short")
    await harness.initialize(token)
    logged = "\n".join(f"{record.getMessage()} {record.__dict__}" for record in caplog.records)
    assert token not in logged
    assert ADMIN_PASSWORD not in logged
    audit = await harness.sql("SELECT details::text, coalesce(user_agent, '') FROM audit_logs")
    assert all(token not in str(row) and ADMIN_PASSWORD not in str(row) for row in audit)
    assert all(ADMIN_EMAIL not in row[0] for row in audit)
