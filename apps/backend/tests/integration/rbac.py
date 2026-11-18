"""Helpers for user, group and session API tests (real PostgreSQL and Redis)."""

from __future__ import annotations

from typing import Any

import httpx

from tests.integration.harness import ADMIN_PASSWORD, Harness

CSRF_HEADER = "X-CSRF-Token"
OTHER_PASSWORD = "another long passphrase 42"


async def login(client: httpx.AsyncClient, email: str, password: str = ADMIN_PASSWORD) -> httpx.Response:
    """Log in and keep the CSRF token on the client, like the frontend keeps it in memory."""
    response = await client.post("/api/v1/auth/login", json={"email": email, "password": password})
    if response.status_code == 200:
        client.headers[CSRF_HEADER] = response.json()["csrf_token"]
    return response


async def setup_admin_client(harness: Harness) -> httpx.AsyncClient:
    """The harness client signed in as the first administrator, with its CSRF token."""
    response = await harness.setup_admin()
    harness.client.headers[CSRF_HEADER] = response.json()["csrf_token"]
    return harness.client


def token_from(url: str) -> str:
    _, _, fragment = url.partition("#token=")
    assert fragment, url
    return fragment


async def invite(client: httpx.AsyncClient, email: str, **extra: Any) -> dict[str, Any]:
    body = {"email": email, "display_name": extra.pop("display_name", "Invited User"), **extra}
    response = await client.post("/api/v1/users", json=body)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


async def activate(client: httpx.AsyncClient, token: str, password: str = ADMIN_PASSWORD) -> httpx.Response:
    response = await client.post("/api/v1/auth/activate", json={"token": token, "password": password})
    if response.status_code == 200:
        client.headers[CSRF_HEADER] = response.json()["csrf_token"]
    return response


async def active_user(harness: Harness, admin: httpx.AsyncClient, email: str, roles: list[str]) -> str:
    """Invite through the API and activate; returns the user id."""
    created = await invite(admin, email, roles=roles)
    async with harness.new_client() as browser:
        response = await activate(browser, token_from(created["activation"]["url"]))
        assert response.status_code == 200, response.text
    user_id: str = created["user"]["id"]
    return user_id


async def audit_actions(harness: Harness) -> list[str]:
    rows = await harness.sql("SELECT action FROM audit_logs ORDER BY created_at, id")
    return [row[0] for row in rows]
