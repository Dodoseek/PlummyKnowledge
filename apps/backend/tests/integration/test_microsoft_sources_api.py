"""``/api/v1/data-sources`` of type ``microsoft_365`` with OAuth against a fake Entra ID on loopback
(API_CONTRACTS.md §2.9, ADR-021 §2, §3, §7).

Real PostgreSQL and Redis, the real ``MicrosoftOAuthClient`` through ``SafeHttpClient`` and the network
policy; only Microsoft is replaced (``tests.fake_microsoft`` checks the client secret, PKCE and single-use
codes).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from tests.fake_http import FakeHttpServer, serve
from tests.fake_microsoft import CLIENT_ID, CLIENT_SECRET, GRAPH, TENANT, FakeMicrosoft, connector_config
from tests.integration.harness import Harness, running_auth_app
from tests.integration.rbac import setup_admin_client
from tests.integration.test_data_sources_api import callback, outcome, problem

pytestmark = pytest.mark.integration

URL = "/api/v1/data-sources"


@dataclass
class Env:
    harness: Harness
    admin: httpx.AsyncClient
    microsoft: FakeMicrosoft
    server: FakeHttpServer
    kb_id: str


@pytest.fixture
async def env(
    migrated_database_url: str, redis_url: str, clean_rate_limits: None, tmp_path: Path
) -> AsyncIterator[Env]:
    microsoft = FakeMicrosoft()
    async with serve(microsoft.handle) as server:
        config = tmp_path / "plummy.toml"
        config.write_text(connector_config(server.port))
        async with running_auth_app(
            migrated_database_url,
            redis_url,
            tmp_path / "bootstrap",
            config_file=config,
            microsoft_oauth_tenant_id=TENANT,
            microsoft_oauth_client_id=CLIENT_ID,
            microsoft_oauth_client_secret=CLIENT_SECRET,
        ) as harness:
            admin = await setup_admin_client(harness)
            provider = await admin.post(
                "/api/v1/providers",
                json={
                    "name": "OpenAI",
                    "provider_type": "openai",
                    "base_url": "https://1.1.1.1/v1",
                    "api_key": "sk-test-not-a-real-key",
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
            kb = await admin.post(
                "/api/v1/knowledge-bases",
                json={"name": "Legal", "embedding_profile_id": profile.json()["id"]},
            )
            assert kb.status_code == 201, kb.text
            yield Env(harness, admin, microsoft, server, str(kb.json()["id"]))


async def create_source(env: Env) -> dict[str, Any]:
    body = {"type": "microsoft_365", "name": "Legal SharePoint", "knowledge_base_id": env.kb_id}
    response = await env.admin.post(URL, json=body)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


async def start(env: Env, source_id: str) -> str:
    response = await env.admin.post(f"{URL}/{source_id}/oauth/start")
    assert response.status_code == 200, response.text
    url: str = response.json()["authorization_url"]
    return url


async def connect(env: Env, source_id: str) -> dict[str, str]:
    code, state = env.microsoft.authorize(await start(env, source_id))
    return outcome(await callback(env.admin, state=state, code=code))


async def test_connect_stores_an_encrypted_refresh_token_and_shows_the_granted_scopes(env: Env) -> None:
    source = await create_source(env)
    assert (source["type"], source["status"], source["credentials"]) == ("microsoft_365", "paused", None)

    url = await start(env, source["id"])
    parts = urlsplit(url)
    query = {key: values[0] for key, values in parse_qs(parts.query).items()}
    assert url.startswith(f"http://127.0.0.1:{env.server.port}/{TENANT}/oauth2/v2.0/authorize?")
    assert query["client_id"] == CLIENT_ID
    assert query["scope"].split() == [
        "offline_access",
        f"{GRAPH}Files.Read.All",
        f"{GRAPH}Sites.Read.All",
        f"{GRAPH}User.Read",
    ]
    assert query["redirect_uri"] == env.harness.settings.oauth_redirect_uri
    assert query["prompt"] == "select_account"
    assert query["code_challenge_method"] == "S256"
    assert "code_verifier" not in url

    code, state = env.microsoft.authorize(url)
    result = outcome(await callback(env.admin, state=state, code=code))
    assert result == {"path": f"/knowledge-bases/{env.kb_id}", "source": source["id"], "oauth": "connected"}
    shown = (await env.admin.get(f"{URL}/{source['id']}")).json()
    assert shown["credentials"]["granted_scopes"] == [
        f"{GRAPH}Files.Read.All",
        f"{GRAPH}Sites.Read.All",
        f"{GRAPH}User.Read",
    ]
    assert shown["credentials"]["account_label"] == "Graph Bot <graph-bot@contoso.example>"
    assert shown["credentials"]["status"] == "valid"

    [refresh] = env.microsoft.refresh_tokens
    rows = await env.harness.sql(
        "SELECT s.kind, s.ciphertext FROM secret_references s "
        "JOIN data_source_credentials c ON c.secret_ref_id = s.id"
    )
    assert len(rows) == 1
    assert rows[0][0] == "oauth_refresh_token"
    assert refresh.encode() not in bytes(rows[0][1])
    exchange = env.microsoft.token_requests[0]
    assert exchange["grant_type"] == "authorization_code"
    secrets_seen = [refresh, code, exchange["code_verifier"], CLIENT_SECRET, *env.microsoft.access_tokens]
    audit = json.dumps(await env.harness.sql("SELECT action, details::text FROM audit_logs"), default=str)
    rendered = json.dumps(shown) + audit
    assert not [value for value in secrets_seen if value in rendered]
    assert "graph-bot@contoso.example" not in audit


@pytest.mark.parametrize(
    "granted",
    [
        f"{GRAPH}Files.Read.All {GRAPH}User.Read",  # Sites.Read.All not consented
        f"{GRAPH}Sites.Selected {GRAPH}User.Read",
        "",
    ],
)
async def test_missing_scope_is_refused_and_nothing_is_stored(env: Env, granted: str) -> None:
    env.microsoft.granted_scope = granted
    source = await create_source(env)
    assert (await connect(env, source["id"]))["reason"] == "oauth_scope_missing"
    assert await env.harness.sql("SELECT count(*) FROM data_source_credentials") == [(0,)]
    assert await env.harness.sql(
        "SELECT count(*) FROM secret_references WHERE kind = 'oauth_refresh_token'"
    ) == [(0,)]


async def test_scopes_are_compared_without_prefix_and_case(env: Env) -> None:
    env.microsoft.granted_scope = "files.read.all SITES.READ.ALL offline_access"
    source = await create_source(env)
    assert (await connect(env, source["id"]))["oauth"] == "connected"


async def test_grant_without_refresh_token_is_refused(env: Env) -> None:
    env.microsoft.issue_refresh_token = False
    source = await create_source(env)
    assert (await connect(env, source["id"]))["reason"] == "oauth_no_refresh_token"
    assert await env.harness.sql("SELECT count(*) FROM data_source_credentials") == [(0,)]


async def test_provider_failure_is_reported_without_details(env: Env) -> None:
    env.microsoft.token_status = 503
    source = await create_source(env)
    result = await connect(env, source["id"])
    assert result["reason"] == "oauth_exchange_failed"
    assert (await env.admin.get(f"{URL}/{source['id']}")).json()["credentials"] is None


async def test_reconnect_replaces_the_stored_token(env: Env) -> None:
    source = await create_source(env)
    await connect(env, source["id"])
    assert (await connect(env, source["id"]))["oauth"] == "connected"
    assert await env.harness.sql("SELECT count(*) FROM data_source_credentials") == [(1,)]
    assert await env.harness.sql(
        "SELECT count(*) FROM secret_references WHERE kind = 'oauth_refresh_token'"
    ) == [(1,)]


async def test_disconnect_deletes_the_credentials_without_a_revoke_call(env: Env) -> None:
    source = await create_source(env)
    await connect(env, source["id"])
    before = len(env.server.requests)
    response = await env.admin.post(f"{URL}/{source['id']}/disconnect")
    assert response.status_code == 200, response.text
    assert (response.json()["status"], response.json()["credentials"]) == ("disconnected", None)
    assert len(env.server.requests) == before  # Microsoft has no endpoint to revoke one token
    assert await env.harness.sql(
        "SELECT count(*) FROM secret_references WHERE kind = 'oauth_refresh_token'"
    ) == [(0,)]
    [(details,)] = await env.harness.sql(
        "SELECT details FROM audit_logs WHERE action = 'data_source.disconnected'"
    )
    assert details["revoked_at_provider"] is False


async def test_settings_show_the_connectors_without_secrets(env: Env) -> None:
    response = await env.admin.get("/api/v1/system/settings")
    connectors = response.json()["connectors"]
    assert connectors["microsoft_365"] == {
        "configured": True,
        "redirect_uri": env.harness.settings.oauth_redirect_uri,
    }
    assert connectors["google_drive"]["configured"] is False
    assert CLIENT_SECRET not in response.text
    assert TENANT not in response.text  # the tenant is not part of the public settings view either


async def test_an_unconfigured_type_next_to_a_configured_one_is_refused(env: Env) -> None:
    response = await env.admin.post(
        URL, json={"type": "google_drive", "name": "X", "knowledge_base_id": env.kb_id}
    )
    problem(response, 409, "connector_not_configured")
