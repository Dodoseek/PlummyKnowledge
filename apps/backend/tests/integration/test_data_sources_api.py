"""``/api/v1/data-sources`` with OAuth against a fake Google on loopback (API_CONTRACTS.md §2.9, ADR-020).

Real PostgreSQL and Redis, the real ``GoogleOAuthClient`` through ``SafeHttpClient`` and the network policy;
only Google is replaced (``tests.fake_google`` checks the client secret, PKCE and single-use codes).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
import pytest

from tests.fake_google import CLIENT_ID, CLIENT_SECRET, DRIVE_READONLY, FakeGoogle, connector_config
from tests.fake_http import FakeHttpServer, serve
from tests.integration.harness import Harness, running_auth_app
from tests.integration.rbac import active_user, login, setup_admin_client

pytestmark = pytest.mark.integration

URL = "/api/v1/data-sources"
FAKE_KEY = "sk-test-not-a-real-key"


@dataclass
class Env:
    harness: Harness
    admin: httpx.AsyncClient
    google: FakeGoogle
    server: FakeHttpServer
    kb_id: str
    profile_id: str


async def _kb(admin: httpx.AsyncClient, name: str, profile_id: str) -> str:
    response = await admin.post(
        "/api/v1/knowledge-bases", json={"name": name, "embedding_profile_id": profile_id}
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


@pytest.fixture
async def env(
    migrated_database_url: str, redis_url: str, clean_rate_limits: None, tmp_path: Path
) -> AsyncIterator[Env]:
    google = FakeGoogle()
    async with serve(google.handle) as server:
        config = tmp_path / "plummy.toml"
        config.write_text(connector_config(server.port))
        async with running_auth_app(
            migrated_database_url,
            redis_url,
            tmp_path / "bootstrap",
            config_file=config,
            google_oauth_client_id=CLIENT_ID,
            google_oauth_client_secret=CLIENT_SECRET,
        ) as harness:
            admin = await setup_admin_client(harness)
            provider = await admin.post(
                "/api/v1/providers",
                json={
                    "name": "OpenAI",
                    "provider_type": "openai",
                    "base_url": "https://1.1.1.1/v1",
                    "api_key": FAKE_KEY,
                },
            )
            assert provider.status_code == 201, provider.text
            profile = await admin.post(
                "/api/v1/embedding-profiles",
                json={
                    "name": "Small",
                    "provider_id": provider.json()["id"],
                    "model_id": "m",
                    "dimensions": 8,
                },
            )
            assert profile.status_code == 201, profile.text
            profile_id = profile.json()["id"]
            yield Env(harness, admin, google, server, await _kb(admin, "Finance", profile_id), profile_id)


def problem(response: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body: dict[str, Any] = response.json()
    assert body["code"] == code, body
    return body


async def create_source(env: Env, client: httpx.AsyncClient | None = None, **extra: Any) -> dict[str, Any]:
    body = {"type": "google_drive", "name": "Team Drive", "knowledge_base_id": env.kb_id, **extra}
    response = await (client or env.admin).post(URL, json=body)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


async def start(env: Env, source_id: str, client: httpx.AsyncClient | None = None) -> str:
    response = await (client or env.admin).post(f"{URL}/{source_id}/oauth/start")
    assert response.status_code == 200, response.text
    url: str = response.json()["authorization_url"]
    return url


async def callback(client: httpx.AsyncClient, **params: str) -> httpx.Response:
    return await client.get(f"{URL}/oauth/callback?{urlencode(params)}")


def outcome(response: httpx.Response) -> dict[str, str]:
    assert response.status_code == 303, response.text
    location = response.headers["location"]
    assert location.startswith("/knowledge-bases"), location  # a path of this application only
    return {key: values[0] for key, values in parse_qs(urlsplit(location).query).items()} | {
        "path": urlsplit(location).path
    }


async def connect(env: Env, source_id: str, client: httpx.AsyncClient | None = None) -> dict[str, str]:
    code, state = env.google.authorize(await start(env, source_id, client))
    return outcome(await callback(client or env.admin, state=state, code=code))


# --- OAuth: the happy path ------------------------------------------------------------------------------


async def test_connect_stores_an_encrypted_refresh_token_and_shows_the_granted_scopes(env: Env) -> None:
    source = await create_source(env)
    assert (source["status"], source["credentials"], source["knowledge_base_id"]) == (
        "paused",
        None,
        env.kb_id,
    )

    url = await start(env, source["id"])
    query = {key: values[0] for key, values in parse_qs(urlsplit(url).query).items()}
    assert url.startswith(f"http://127.0.0.1:{env.server.port}/o/oauth2/v2/auth?")
    assert query["scope"] == DRIVE_READONLY
    assert query["redirect_uri"] == env.harness.settings.oauth_redirect_uri
    assert query["prompt"] == "consent"
    assert query["include_granted_scopes"] == "false"
    assert "code_verifier" not in url  # only the challenge leaves the server

    code, state = env.google.authorize(url)
    result = outcome(await callback(env.admin, state=state, code=code))
    assert result == {
        "path": f"/knowledge-bases/{env.kb_id}",
        "source": source["id"],
        "oauth": "connected",
    }
    shown = (await env.admin.get(f"{URL}/{source['id']}")).json()
    assert shown["credentials"]["granted_scopes"] == [DRIVE_READONLY]
    assert shown["credentials"]["account_label"] == "Drive Bot <drive-bot@example.com>"
    assert shown["credentials"]["status"] == "valid"
    assert shown["status"] == "paused"  # no folder chosen yet

    [refresh] = env.google.refresh_tokens
    rows = await env.harness.sql(
        "SELECT s.kind, s.ciphertext FROM secret_references s "
        "JOIN data_source_credentials c ON c.secret_ref_id = s.id"
    )
    assert len(rows) == 1
    assert rows[0][0] == "oauth_refresh_token"
    assert refresh.encode() not in bytes(rows[0][1])
    exchange = env.google.token_requests[0]
    assert exchange["grant_type"] == "authorization_code"
    assert exchange["code_verifier"]

    secrets_seen = [refresh, code, exchange["code_verifier"], CLIENT_SECRET, *env.google.access_tokens]
    audit = json.dumps(await env.harness.sql("SELECT action, details::text FROM audit_logs"), default=str)
    rendered = json.dumps(shown) + audit
    assert not [value for value in secrets_seen if value in rendered]
    assert "data_source.connected" in audit
    assert "drive-bot@example.com" not in audit


# --- OAuth: state, session and provider failures ------------------------------------------------------


async def test_state_is_single_use(env: Env) -> None:
    source = await create_source(env)
    code, state = env.google.authorize(await start(env, source["id"]))
    assert outcome(await callback(env.admin, state=state, code=code))["oauth"] == "connected"
    replay = outcome(await callback(env.admin, state=state, code=code))
    assert replay == {"path": "/knowledge-bases", "oauth": "failed", "reason": "oauth_state_invalid"}
    assert len(env.google.token_requests) == 1


@pytest.mark.parametrize("state", ["", "short", "x" * 129, "bad state with spaces 0123456789", "a" * 43])
async def test_forged_or_malformed_state_is_refused_without_an_exchange(env: Env, state: str) -> None:
    await create_source(env)
    result = outcome(await callback(env.admin, state=state, code="code-anything"))
    assert (result["oauth"], result["reason"]) == ("failed", "oauth_state_invalid")
    assert env.google.token_requests == []


async def test_state_of_another_session_is_refused_and_burnt(env: Env) -> None:
    """Login CSRF / code injection: the callback must arrive in the session that started the flow."""
    source = await create_source(env)
    code, state = env.google.authorize(await start(env, source["id"]))
    async with env.harness.new_client() as other_browser:
        assert (await login(other_browser, "admin@example.com")).status_code == 200
        result = outcome(await callback(other_browser, state=state, code=code))
    assert (result["path"], result["reason"]) == ("/knowledge-bases", "oauth_session_mismatch")
    # The state is gone: the right session cannot reuse it after it leaked.
    assert outcome(await callback(env.admin, state=state, code=code))["reason"] == "oauth_state_invalid"
    assert env.google.token_requests == []
    rows = await env.harness.sql(
        "SELECT outcome, details->>'reason' FROM audit_logs WHERE action = 'data_source.oauth.failed'"
    )
    assert rows == [("denied", "oauth_session_mismatch")]


async def test_callback_without_a_session_is_refused(env: Env) -> None:
    source = await create_source(env)
    code, state = env.google.authorize(await start(env, source["id"]))
    async with env.harness.new_client() as anonymous:
        result = outcome(await callback(anonymous, state=state, code=code))
    assert result["reason"] == "oauth_session_mismatch"
    rows = await env.harness.sql(
        "SELECT actor_type FROM audit_logs WHERE action = 'data_source.oauth.failed'"
    )
    assert rows == [("anonymous",)]
    assert env.google.token_requests == []


async def test_denied_consent_and_provider_errors(env: Env) -> None:
    source = await create_source(env)
    _, state = env.google.authorize(await start(env, source["id"]))
    result = outcome(await callback(env.admin, state=state, error="access_denied"))
    assert result == {
        "path": f"/knowledge-bases/{env.kb_id}",
        "source": source["id"],
        "oauth": "failed",
        "reason": "oauth_denied",
    }
    _, state = env.google.authorize(await start(env, source["id"]))
    assert (
        outcome(await callback(env.admin, state=state, error="<script>"))["reason"] == "oauth_provider_error"
    )
    _, state = env.google.authorize(await start(env, source["id"]))
    assert outcome(await callback(env.admin, state=state))["reason"] == "oauth_code_missing"
    assert (await env.admin.get(f"{URL}/{source['id']}")).json()["credentials"] is None


async def test_wrong_code_is_refused_by_the_provider(env: Env) -> None:
    source = await create_source(env)
    _, state = env.google.authorize(await start(env, source["id"]))
    result = outcome(await callback(env.admin, state=state, code="code-forged"))
    assert result["reason"] == "oauth_exchange_failed"
    assert (await env.admin.get(f"{URL}/{source['id']}")).json()["credentials"] is None


async def test_missing_scope_is_refused_and_the_token_revoked(env: Env) -> None:
    env.google.granted_scope = "https://www.googleapis.com/auth/drive.metadata.readonly"
    source = await create_source(env)
    assert (await connect(env, source["id"]))["reason"] == "oauth_scope_missing"
    assert len(env.google.revoked) == 1
    assert env.google.refresh_tokens == set()
    assert await env.harness.sql("SELECT count(*) FROM data_source_credentials") == [(0,)]


async def test_grant_without_refresh_token_is_refused(env: Env) -> None:
    env.google.issue_refresh_token = False
    source = await create_source(env)
    assert (await connect(env, source["id"]))["reason"] == "oauth_no_refresh_token"
    assert await env.harness.sql("SELECT count(*) FROM secret_references") == [(1,)]  # only the provider key


async def test_rights_lost_between_start_and_callback_are_checked(env: Env) -> None:
    source = await create_source(env)
    code, state = env.google.authorize(await start(env, source["id"]))
    await env.admin.delete(f"/api/v1/knowledge-bases/{env.kb_id}")
    result = outcome(await callback(env.admin, state=state, code=code))
    assert (result["path"], result["reason"]) == ("/knowledge-bases", "oauth_forbidden")
    assert env.google.token_requests == []


async def test_reconnect_replaces_the_secret_and_revokes_the_old_token(env: Env) -> None:
    source = await create_source(env)
    await connect(env, source["id"])
    [first] = env.google.refresh_tokens
    assert (await connect(env, source["id"]))["oauth"] == "connected"
    assert first in env.google.revoked
    assert await env.harness.sql("SELECT count(*) FROM data_source_credentials") == [(1,)]
    assert await env.harness.sql(
        "SELECT count(*) FROM secret_references WHERE kind = 'oauth_refresh_token'"
    ) == [(1,)]


# --- disconnect and delete ------------------------------------------------------------------------------


async def test_disconnect_revokes_and_deletes_the_credentials(env: Env) -> None:
    source = await create_source(env)
    await connect(env, source["id"])
    [refresh] = env.google.refresh_tokens
    response = await env.admin.post(f"{URL}/{source['id']}/disconnect")
    assert response.status_code == 200, response.text
    assert (response.json()["status"], response.json()["credentials"]) == ("disconnected", None)
    assert refresh in env.google.revoked
    assert await env.harness.sql(
        "SELECT count(*) FROM secret_references WHERE kind = 'oauth_refresh_token'"
    ) == [(0,)]
    [(details,)] = await env.harness.sql(
        "SELECT details FROM audit_logs WHERE action = 'data_source.disconnected'"
    )
    assert details["revoked_at_provider"] is True


async def test_disconnect_when_google_is_down_still_removes_the_credentials(env: Env) -> None:
    source = await create_source(env)
    await connect(env, source["id"])
    env.google.revoke_status = 503
    response = await env.admin.post(f"{URL}/{source['id']}/disconnect")
    assert response.status_code == 200, response.text
    assert await env.harness.sql("SELECT count(*) FROM data_source_credentials") == [(0,)]
    [(details,)] = await env.harness.sql(
        "SELECT details FROM audit_logs WHERE action = 'data_source.disconnected'"
    )
    assert details["revoked_at_provider"] is False


async def _document(env: Env, source_id: str, external_id: str) -> str:
    rows = await env.harness.sql(
        "INSERT INTO documents (id, data_source_id, external_id, filename) "
        "VALUES (gen_random_uuid(), :source, :external, 'report.pdf') RETURNING id",
        source=source_id,
        external=external_id,
    )
    return str(rows[0][0])


async def test_delete_removes_documents_credentials_and_the_link(env: Env) -> None:
    source = await create_source(env)
    await connect(env, source["id"])
    document = await _document(env, source["id"], "drive-file-1")
    response = await env.admin.delete(f"{URL}/{source['id']}")
    assert response.status_code == 200, response.text
    assert response.json() == {"documents_deleted": 1}
    assert await env.harness.sql("SELECT deletion_reason FROM documents WHERE id = :id", id=document) == [
        ("source_deleted",)
    ]
    assert await env.harness.sql("SELECT count(*) FROM data_source_credentials") == [(0,)]
    assert await env.harness.sql(
        "SELECT count(*) FROM knowledge_base_sources WHERE data_source_id = :id", id=source["id"]
    ) == [(0,)]
    assert len(env.google.revoked) == 1
    problem(await env.admin.get(f"{URL}/{source['id']}"), 404, "not_found")
    ids = [item["id"] for item in (await env.admin.get(URL)).json()["items"]]
    assert source["id"] not in ids


async def test_deleting_the_knowledge_base_takes_its_external_sources(env: Env) -> None:
    source = await create_source(env)
    await connect(env, source["id"])
    document = await _document(env, source["id"], "drive-file-2")
    assert (await env.admin.delete(f"/api/v1/knowledge-bases/{env.kb_id}")).status_code == 204
    assert await env.harness.sql(
        "SELECT deleted_at IS NOT NULL FROM data_sources WHERE id = :id", id=source["id"]
    ) == [(True,)]
    assert await env.harness.sql("SELECT deletion_reason FROM documents WHERE id = :id", id=document) == [
        ("kb_deleted",)
    ]
    assert await env.harness.sql("SELECT count(*) FROM data_source_credentials") == [(0,)]
    assert len(env.google.revoked) == 1


# --- create, update, list and the upload source ----------------------------------------------------------


async def test_upload_sources_are_listed_but_managed_by_their_knowledge_base(env: Env) -> None:
    listed = (await env.admin.get(URL)).json()["items"]
    [upload] = [item for item in listed if item["type"] == "upload"]
    assert upload["owner_knowledge_base_id"] == env.kb_id
    assert upload["sync"] is None
    problem(await env.admin.delete(f"{URL}/{upload['id']}"), 409, "upload_source_managed_by_kb")
    problem(
        await env.admin.patch(f"{URL}/{upload['id']}", json={"name": "X"}), 409, "upload_source_managed_by_kb"
    )
    problem(await env.admin.post(f"{URL}/{upload['id']}/oauth/start"), 409, "upload_source_managed_by_kb")
    problem(await env.admin.post(f"{URL}/{upload['id']}/disconnect"), 409, "upload_source_managed_by_kb")
    response = await env.admin.post(URL, json={"type": "upload", "name": "U", "knowledge_base_id": env.kb_id})
    assert problem(response, 422, "validation_error")["errors"][0]["code"] == "source_type_not_allowed"


async def test_create_and_update_validate_input(env: Env) -> None:
    source = await create_source(env, name="  Team   Drive ", sync_interval_minutes=None)
    assert source["name"] == "Team Drive"
    assert source["sync"]["interval_minutes"] is None
    for interval in (4, 1441, 0):
        response = await env.admin.patch(f"{URL}/{source['id']}", json={"sync_interval_minutes": interval})
        assert problem(response, 422, "validation_error")["errors"][0]["code"] == "invalid_sync_interval"
    updated = await env.admin.patch(
        f"{URL}/{source['id']}", json={"sync_interval_minutes": 60, "name": "Drive"}
    )
    assert (updated.json()["name"], updated.json()["sync"]["interval_minutes"]) == ("Drive", 60)
    kept = await env.admin.patch(f"{URL}/{source['id']}", json={"name": "Drive 2"})
    assert kept.json()["sync"]["interval_minutes"] == 60  # an omitted field is left alone
    cleared = await env.admin.patch(f"{URL}/{source['id']}", json={"sync_interval_minutes": None})
    assert cleared.json()["sync"]["interval_minutes"] is None
    assert (await env.admin.patch(f"{URL}/{source['id']}", json={"config": {}})).status_code == 422
    response = await env.admin.post(
        URL,
        json={
            "type": "google_drive",
            "name": "X",
            "knowledge_base_id": "00000000-0000-0000-0000-000000000000",
        },
    )
    problem(response, 404, "not_found")


async def test_sources_of_inaccessible_knowledge_bases_are_invisible(env: Env) -> None:
    source = await create_source(env)
    await active_user(env.harness, env.admin, "member@example.com", ["member"])
    await env.harness.sql(
        "INSERT INTO roles (id, key, name, description, is_system) "
        "VALUES (gen_random_uuid(), 'drive_reader', 'R', 'Drive reader', false)"
    )
    await env.harness.sql(
        "INSERT INTO role_permissions (role_id, permission_id) SELECT r.id, p.id FROM roles r, permissions p "
        "WHERE r.key = 'drive_reader' AND p.key IN ('data_sources.read', 'data_sources.manage')"
    )
    await env.harness.sql(
        "INSERT INTO user_roles (user_id, role_id) SELECT u.id, r.id FROM users u, roles r "
        "WHERE u.email = 'member@example.com' AND r.key = 'drive_reader'"
    )
    async with env.harness.new_client() as member:
        assert (await login(member, "member@example.com")).status_code == 200
        assert (await member.get(URL)).json()["items"] == []
        problem(await member.get(f"{URL}/{source['id']}"), 404, "not_found")
        problem(await member.post(f"{URL}/{source['id']}/oauth/start"), 404, "not_found")
        problem(await member.delete(f"{URL}/{source['id']}"), 404, "not_found")
        response = await member.post(
            URL, json={"type": "google_drive", "name": "X", "knowledge_base_id": env.kb_id}
        )
        problem(response, 404, "not_found")
        await env.harness.sql(
            "INSERT INTO knowledge_base_access "
            "(id, knowledge_base_id, principal_type, user_id, access_level) "
            "SELECT gen_random_uuid(), :kb, 'user', id, 'read' FROM users WHERE email = 'member@example.com'",
            kb=env.kb_id,
        )
        assert source["id"] in [item["id"] for item in (await member.get(URL)).json()["items"]]
        problem(await member.post(f"{URL}/{source['id']}/oauth/start"), 403, "permission_denied")


async def test_knowledge_manager_reads_but_cannot_manage(env: Env) -> None:
    source = await create_source(env)
    await active_user(env.harness, env.admin, "km@example.com", ["knowledge_manager"])
    async with env.harness.new_client() as manager:
        assert (await login(manager, "km@example.com")).status_code == 200
        assert (await manager.get(f"{URL}/{source['id']}")).status_code == 200
        problem(await manager.post(f"{URL}/{source['id']}/oauth/start"), 403, "permission_denied")
        problem(await manager.post(f"{URL}/{source['id']}/disconnect"), 403, "permission_denied")
        response = await manager.post(
            URL, json={"type": "google_drive", "name": "X", "knowledge_base_id": env.kb_id}
        )
        problem(response, 403, "permission_denied")
    async with env.harness.new_client() as anonymous:
        problem(await anonymous.get(URL), 401, "unauthenticated")


async def test_state_changing_calls_need_csrf(env: Env) -> None:
    source = await create_source(env)
    async with env.harness.new_client() as browser:
        assert (await login(browser, "admin@example.com")).status_code == 200
        del browser.headers["X-CSRF-Token"]
        assert (await browser.post(f"{URL}/{source['id']}/oauth/start")).status_code == 403
        assert (await browser.delete(f"{URL}/{source['id']}")).status_code == 403


async def test_settings_show_the_connector_without_secrets(env: Env) -> None:
    response = await env.admin.get("/api/v1/system/settings")
    assert response.json()["connectors"]["google_drive"] == {
        "configured": True,
        "redirect_uri": env.harness.settings.oauth_redirect_uri,
    }
    assert CLIENT_SECRET not in response.text


async def test_unconfigured_connector_is_refused(
    migrated_database_url: str, redis_url: str, clean_rate_limits: None, tmp_path: Path
) -> None:
    async with running_auth_app(migrated_database_url, redis_url, tmp_path / "bootstrap") as harness:
        admin = await setup_admin_client(harness)
        kbs = (await admin.get("/api/v1/data-sources")).json()["items"]
        assert kbs == []
        response = await admin.post(
            URL,
            json={
                "type": "google_drive",
                "name": "X",
                "knowledge_base_id": "00000000-0000-0000-0000-000000000000",
            },
        )
        problem(response, 409, "connector_not_configured")
        settings = (await admin.get("/api/v1/system/settings")).json()
        assert settings["connectors"]["google_drive"]["configured"] is False
