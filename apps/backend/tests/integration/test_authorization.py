"""Authorization foundation: SecurityContext from the session only, permission checks (SECURITY_MODEL §3)."""

from __future__ import annotations

import httpx
import pytest
from fastapi import APIRouter, Depends

from plummy.api.security import CurrentContext, require_permission
from plummy.application.security import SecurityContext
from plummy.domain.identity import Permission
from tests.integration.harness import ADMIN_PASSWORD, Harness

pytestmark = pytest.mark.integration

MEMBER_EMAIL = "member@example.com"


def _probe_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/_probe")

    @router.get("/whoami")
    async def whoami(context: CurrentContext) -> dict[str, object]:
        return {"user_id": str(context.user_id), "permissions": sorted(context.permissions)}

    @router.get("/audit")
    async def audit(
        context: SecurityContext = Depends(require_permission(Permission.AUDIT_READ)),  # noqa: B008
    ) -> dict[str, str]:
        return {"user_id": str(context.user_id)}

    @router.post("/change")
    async def change(context: CurrentContext) -> dict[str, str]:
        return {"user_id": str(context.user_id)}

    return router


@pytest.fixture
async def probe(harness: Harness) -> Harness:
    await harness.setup_admin()
    harness.app.include_router(_probe_router())
    return harness


async def _login(client: httpx.AsyncClient, email: str) -> str:
    response = await client.post("/api/v1/auth/login", json={"email": email, "password": ADMIN_PASSWORD})
    assert response.status_code == 200, response.text
    csrf: str = response.json()["csrf_token"]
    return csrf


async def test_anonymous_request_is_unauthenticated(probe: Harness) -> None:
    async with probe.new_client() as anonymous:
        assert (await anonymous.get("/api/v1/_probe/whoami")).status_code == 401
        assert (await anonymous.get("/api/v1/_probe/audit")).json()["code"] == "unauthenticated"


async def test_member_lacks_admin_permission(probe: Harness) -> None:
    await probe.create_user(MEMBER_EMAIL, ADMIN_PASSWORD, "member")
    async with probe.new_client() as client:
        await _login(client, MEMBER_EMAIL)
        whoami = await client.get("/api/v1/_probe/whoami")
        audit = await client.get("/api/v1/_probe/audit")
    assert whoami.json()["permissions"] == ["chat.use"]
    assert audit.status_code == 403
    assert audit.json()["code"] == "permission_denied"


async def test_user_without_roles_has_no_permissions(probe: Harness) -> None:
    await probe.create_user("plain@example.com", ADMIN_PASSWORD, None)
    async with probe.new_client() as client:
        await _login(client, "plain@example.com")
        me = await client.get("/api/v1/auth/me")
        audit = await client.get("/api/v1/_probe/audit")
    assert me.json()["roles"] == []
    assert me.json()["permissions"] == []
    assert audit.status_code == 403


async def test_auditor_and_admin_have_audit_read(probe: Harness) -> None:
    await probe.create_user("auditor@example.com", ADMIN_PASSWORD, "auditor")
    async with probe.new_client() as client:
        await _login(client, "auditor@example.com")
        assert (await client.get("/api/v1/_probe/audit")).status_code == 200
    assert (await probe.client.get("/api/v1/_probe/audit")).status_code == 200


async def test_role_revocation_applies_to_the_next_request(probe: Harness) -> None:
    user_id = await probe.create_user("auditor@example.com", ADMIN_PASSWORD, "auditor")
    async with probe.new_client() as client:
        await _login(client, "auditor@example.com")
        assert (await client.get("/api/v1/_probe/audit")).status_code == 200
        await probe.sql("DELETE FROM user_roles WHERE user_id = CAST(:id AS uuid)", id=user_id)
        assert (await client.get("/api/v1/_probe/audit")).status_code == 403


async def test_client_supplied_identity_is_ignored(probe: Harness) -> None:
    member_id = await probe.create_user(MEMBER_EMAIL, ADMIN_PASSWORD, "member")
    async with probe.new_client() as client:
        await _login(client, MEMBER_EMAIL)
        response = await client.get(
            "/api/v1/_probe/audit?user_id=x&role=admin",
            headers={"X-User-Id": "admin", "X-Roles": "admin", "X-Permissions": "audit.read"},
        )
        whoami = await client.get("/api/v1/_probe/whoami")
    assert response.status_code == 403
    assert whoami.json()["user_id"] == member_id


async def test_unsafe_methods_require_csrf_token(probe: Harness) -> None:
    async with probe.new_client() as client:
        csrf = await _login(client, "admin@example.com")
        without = await client.post("/api/v1/_probe/change")
        with_token = await client.post("/api/v1/_probe/change", headers={"X-CSRF-Token": csrf})
    assert without.status_code == 403
    assert without.json()["code"] == "csrf_failed"
    assert with_token.status_code == 200
