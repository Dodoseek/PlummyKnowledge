"""``GET /api/v1/audit/events`` on real PostgreSQL and Redis (API_CONTRACTS.md §2.16, Phase 12.5)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.integration.harness import ADMIN_EMAIL, ADMIN_PASSWORD, Harness, running_auth_app
from tests.integration.rbac import activate, invite, login, setup_admin_client, token_from

pytestmark = pytest.mark.integration

URL = "/api/v1/audit/events"
MISSING = "00000000-0000-7000-8000-000000000000"
WRONG_PASSWORD = "audit-wrong-password-Qe40"
PROVIDER_KEY = "sk-audit-provider-key-Nn3v0000000000000000"  # secret-scan: allow (fake test value)


@dataclass
class Env:
    harness: Harness
    admin: httpx.AsyncClient
    auditor: httpx.AsyncClient
    member: httpx.AsyncClient
    auditor_id: str
    secrets: list[str]


async def signed_in(
    harness: Harness, admin: httpx.AsyncClient, email: str, role: str
) -> tuple[Any, str, str]:
    created = await invite(admin, email, roles=[role])
    token = token_from(created["activation"]["url"])
    client = harness.new_client()
    assert (await activate(client, token)).status_code == 200
    return client, created["user"]["id"], token


@pytest.fixture
async def env(
    migrated_database_url: str, redis_url: str, clean_rate_limits: None, tmp_path: Path
) -> AsyncIterator[Env]:
    async with running_auth_app(migrated_database_url, redis_url, tmp_path / "bootstrap") as harness:
        admin = await setup_admin_client(harness)
        auditor, auditor_id, auditor_token = await signed_in(harness, admin, "auditor@example.com", "auditor")
        member, _, member_token = await signed_in(harness, admin, "member@example.com", "member")
        async with harness.new_client() as stranger:
            assert (await login(stranger, ADMIN_EMAIL, WRONG_PASSWORD)).status_code == 401
        provider = await admin.post(
            "/api/v1/providers",
            json={
                "name": "Audit",
                "provider_type": "openai",
                "base_url": "https://1.1.1.1/v1",
                "api_key": PROVIDER_KEY,
            },
        )
        assert provider.status_code == 201, provider.text
        secrets = [ADMIN_PASSWORD, WRONG_PASSWORD, PROVIDER_KEY, auditor_token, member_token]
        secrets += [
            cookie.value for client in (admin, auditor) for cookie in client.cookies.jar if cookie.value
        ]
        yield Env(harness, admin, auditor, member, auditor_id, secrets)
        await auditor.aclose()
        await member.aclose()


async def all_events(client: httpx.AsyncClient, **params: str) -> list[dict[str, Any]]:
    response = await client.get(URL, params={**params, "limit": "100"})
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return items


async def test_the_auditor_and_the_administrator_read_the_log(env: Env) -> None:
    for client in (env.auditor, env.admin):
        events = await all_events(client)
        actions = {event["action"] for event in events}
        assert {"setup.completed", "user.created", "auth.login.failed"} <= actions
        response = await client.get(URL)
        assert response.headers["cache-control"] == "no-store"
    newest_first = [event["created_at"] for event in await all_events(env.auditor)]
    assert newest_first == sorted(newest_first, reverse=True)


async def test_one_event_by_id(env: Env) -> None:
    [event, *_] = await all_events(env.auditor)
    response = await env.auditor.get(f"{URL}/{event['id']}")
    assert response.status_code == 200
    assert response.json() == event
    assert (await env.auditor.get(f"{URL}/{MISSING}")).status_code == 404
    assert (await env.auditor.get(f"{URL}/not-a-uuid")).status_code == 422


async def test_without_audit_read_nothing_is_shown(env: Env) -> None:
    [event, *_] = await all_events(env.auditor)
    for path in (URL, f"{URL}/{event['id']}", f"{URL}/{MISSING}"):
        response = await env.member.get(path)
        assert response.status_code == 403
        assert response.json()["code"] == "permission_denied"
    async with env.harness.new_client() as anonymous:
        assert (await anonymous.get(URL)).status_code == 401


async def test_the_log_cannot_be_changed_through_the_api(env: Env) -> None:
    [event, *_] = await all_events(env.admin)
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        for path in (URL, f"{URL}/{event['id']}"):
            assert (await env.admin.request(method, path)).status_code == 405, (method, path)
    rows = await env.harness.sql(
        "SELECT count(*) FROM audit_logs WHERE id = CAST(:id AS uuid)", id=event["id"]
    )
    assert rows == [(1,)]


async def test_no_secret_is_returned(env: Env) -> None:
    text = (await env.admin.get(URL, params={"limit": "100"})).text
    assert len(await all_events(env.admin)) > 5
    assert [secret for secret in env.secrets if secret in text] == []


async def test_filters(env: Env) -> None:
    failed = await all_events(env.auditor, action="auth.login.failed", outcome="denied")
    assert failed
    assert all(e["action"] == "auth.login.failed" and e["outcome"] == "denied" for e in failed)
    mine = await all_events(env.auditor, actor_user_id=env.auditor_id)
    assert mine
    assert {e["actor_user_id"] for e in mine} == {env.auditor_id}
    created = await all_events(env.auditor, action="user.created")
    target = created[0]
    by_target = await all_events(
        env.auditor, target_type=target["target_type"], target_id=target["target_id"]
    )
    assert target["id"] in {e["id"] for e in by_target}
    assert {(e["target_type"], e["target_id"]) for e in by_target} == {
        (target["target_type"], target["target_id"])
    }
    assert await all_events(env.auditor, action="no.such.action") == []


async def test_time_range(env: Env) -> None:
    events = await all_events(env.auditor, sort="created_at")
    middle = datetime.fromisoformat(events[len(events) // 2]["created_at"])
    before = await all_events(env.auditor, to=middle.isoformat())
    after = await all_events(env.auditor, **{"from": middle.isoformat()})
    assert len(before) + len(after) == len(events)  # ``from`` inclusive, ``to`` exclusive
    assert all(datetime.fromisoformat(e["created_at"]) < middle for e in before)
    future = (datetime.now(UTC) + timedelta(days=1)).isoformat()
    assert await all_events(env.auditor, **{"from": future}) == []
    inverted = await env.auditor.get(URL, params={"from": future, "to": middle.isoformat()})
    assert inverted.status_code == 422
    assert inverted.json()["code"] == "invalid_query_parameter"
    naive = await env.auditor.get(URL, params={"from": "2026-01-01T00:00:00"})
    assert naive.status_code == 422


async def test_pages_walk_the_whole_log_once(env: Env) -> None:
    every = [e["id"] for e in await all_events(env.auditor)]
    seen: list[str] = []
    cursor: str | None = None
    while True:
        params = {"limit": "3", **({"cursor": cursor} if cursor else {})}
        body = (await env.auditor.get(URL, params=params)).json()
        seen += [e["id"] for e in body["items"]]
        if not body["has_more"]:
            break
        cursor = body["next_cursor"]
    assert seen == every


async def test_unknown_query_parameters_and_bad_values_are_rejected(env: Env) -> None:
    for params in (
        {"details": "x"},
        {"outcome": "maybe"},
        {"actor_user_id": "x"},
        {"sort": "action"},
        {"limit": "0"},
    ):
        response = await env.auditor.get(URL, params=params)
        assert response.status_code in (400, 422), (params, response.text)
