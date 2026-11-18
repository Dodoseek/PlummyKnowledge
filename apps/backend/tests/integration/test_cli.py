"""Server CLI (``python -m plummy.cli``): bootstrap token, admin create, password reset (ADR-006)."""

from __future__ import annotations

import io

import pytest

from plummy.cli.app import main
from plummy.composition import build_cli_services
from tests.integration.harness import ADMIN_EMAIL, ADMIN_PASSWORD, Harness, fast_hasher

pytestmark = pytest.mark.integration

NEW_PASSWORD = "another long passphrase"


def _run(harness: Harness, *argv: str, stdin: str = "") -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(
        list(argv),
        services_factory=lambda s: build_cli_services(s, password_hasher=fast_hasher()),
        settings=harness.settings,
        stdout=out,
        stderr=err,
        stdin=io.StringIO(stdin),
    )
    return code, out.getvalue(), err.getvalue()


async def _in_thread(harness: Harness, *argv: str, stdin: str = "") -> tuple[int, str, str]:
    import asyncio

    # main() calls asyncio.run(); run it outside the test's event loop.
    return await asyncio.to_thread(_run, harness, *argv, stdin=stdin)


async def test_init_token_prints_only_the_path(harness: Harness) -> None:
    code, out, _ = await _in_thread(harness, "setup", "init-token")
    token = (harness.settings.bootstrap_dir / "setup-token").read_text().strip()
    assert code == 0
    assert str(harness.settings.bootstrap_dir / "setup-token") in out
    assert token not in out

    code, out, _ = await _in_thread(harness, "setup", "status")
    assert "initialized: no" in out
    assert "present" in out


async def test_admin_create_initializes_instance_and_spends_token(harness: Harness) -> None:
    await _in_thread(harness, "setup", "init-token")
    token = (harness.settings.bootstrap_dir / "setup-token").read_text().strip()

    code, out, err = await _in_thread(
        harness,
        "admin",
        "create",
        "--email",
        ADMIN_EMAIL,
        "--display-name",
        "Ops",
        "--password-stdin",
        stdin=ADMIN_PASSWORD + "\n",
    )
    assert code == 0, err
    assert "Instance initialized" in out
    assert not (harness.settings.bootstrap_dir / "setup-token").exists()
    assert (await harness.initialize(token)).status_code == 409
    login = await harness.client.post(
        "/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    )
    assert login.status_code == 200
    assert login.json()["roles"] == ["admin"]


async def test_admin_create_rejects_duplicates_and_weak_passwords(harness: Harness) -> None:
    await harness.setup_admin()
    code, _, err = await _in_thread(
        harness,
        "admin",
        "create",
        "--email",
        ADMIN_EMAIL,
        "--display-name",
        "X",
        "--password-stdin",
        stdin=NEW_PASSWORD + "\n",
    )
    assert code == 1
    assert "already exists" in err
    code, _, err = await _in_thread(
        harness,
        "admin",
        "create",
        "--email",
        "b@example.com",
        "--display-name",
        "X",
        "--password-stdin",
        stdin="short\n",
    )
    assert code == 1
    assert "password_too_short" not in err
    assert "password" in err
    assert await harness.sql("SELECT count(*) FROM users") == [(1,)]


async def test_reset_password_revokes_all_sessions(harness: Harness) -> None:
    await harness.setup_admin()  # harness.client holds a session now
    assert (await harness.client.get("/api/v1/auth/me")).status_code == 200

    code, out, err = await _in_thread(
        harness,
        "admin",
        "reset-password",
        "--email",
        ADMIN_EMAIL,
        "--password-stdin",
        stdin=NEW_PASSWORD + "\n",
    )
    assert code == 0, err
    assert "1 session(s) revoked" in out
    assert NEW_PASSWORD not in out + err

    me = await harness.client.get("/api/v1/auth/me")
    assert me.status_code == 401
    assert me.json()["code"] == "session_revoked"
    old = await harness.client.post(
        "/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    )
    new = await harness.client.post(
        "/api/v1/auth/login", json={"email": ADMIN_EMAIL, "password": NEW_PASSWORD}
    )
    assert old.status_code == 401
    assert new.status_code == 200
    audit = await harness.sql("SELECT count(*) FROM audit_logs WHERE action = 'admin.password_reset_via_cli'")
    assert audit == [(1,)]


async def test_reset_password_for_unknown_user_fails(harness: Harness) -> None:
    code, _, err = await _in_thread(
        harness,
        "admin",
        "reset-password",
        "--email",
        "nobody@example.com",
        "--password-stdin",
        stdin=NEW_PASSWORD + "\n",
    )
    assert code == 1
    assert "No user" in err


def test_password_is_never_accepted_as_an_argument() -> None:
    with pytest.raises(SystemExit):
        main(["admin", "create", "--email", "a@example.com", "--display-name", "A", "--password", "secret"])
