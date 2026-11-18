"""``/api/v1/providers`` and ``/api/v1/embedding-profiles`` on real PostgreSQL and Redis (§2.7-2.8).

A fake provider runs on loopback; the instance allowlist admits it only for the listed provider types, as an
operator would admit a local Ollama (SC-06). Keys are write-only (§12 "secrets are not returned").
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.fake_http import FakeHttpServer, ReceivedRequest, Reply, serve
from tests.integration.harness import Harness, running_auth_app
from tests.integration.rbac import active_user, audit_actions, login, setup_admin_client

pytestmark = pytest.mark.integration

OPENAI_KEY = "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789WXYZ"  # fake test value; secret-scan: allow
OTHER_KEY = "sk-proj-zyxwvutsrqponmlkjihgfedcba9876543210ABCD"  # fake test value; secret-scan: allow
LEAK = "upstream says: invalid key sk-proj-abcdefghijklmnop (request req_123)"


@dataclass
class FakeProvider:
    server: FakeHttpServer
    handler: Callable[[ReceivedRequest], Reply] = field(default=lambda _: Reply.json({"models": []}))

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.port}"


@dataclass
class Env:
    harness: Harness
    admin: httpx.AsyncClient
    fake: FakeProvider


@pytest.fixture
async def env(
    migrated_database_url: str, redis_url: str, clean_rate_limits: None, tmp_path: Path
) -> AsyncIterator[Env]:
    holder: list[FakeProvider] = []
    async with serve(lambda request: holder[0].handler(request)) as server:
        fake = FakeProvider(server)
        holder.append(fake)
        config = tmp_path / "plummy.toml"
        config.write_text(
            f"""
[[network.allowlist]]
name = "local-models"
url = "{fake.url}"
purposes = ["llm", "embedding"]
provider_types = ["ollama", "openai_compatible"]
allowed_cidrs = ["127.0.0.1/32"]
"""
        )
        async with running_auth_app(
            migrated_database_url, redis_url, tmp_path / "bootstrap", config_file=config
        ) as harness:
            yield Env(harness, await setup_admin_client(harness), fake)


async def create(admin: httpx.AsyncClient, **body: Any) -> dict[str, Any]:
    response = await admin.post("/api/v1/providers", json=body)
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


def public_openai(**extra: Any) -> dict[str, Any]:
    # A public IP literal: passes the policy without DNS and is never contacted by these tests.
    return {
        "name": "OpenAI",
        "provider_type": "openai",
        "base_url": "https://1.1.1.1/v1",
        "api_key": OPENAI_KEY,
        **extra,
    }


def assert_problem(response: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body: dict[str, Any] = response.json()
    assert body["code"] == code, body
    return body


# --- create / read: secrets are write-only --------------------------------------------------------------


async def test_created_provider_never_returns_or_stores_the_key_in_clear(env: Env) -> None:
    response = await env.admin.post("/api/v1/providers", json=public_openai(settings={"timeout_seconds": 45}))
    assert response.status_code == 201, response.text
    body = response.json()
    assert body == {
        "id": body["id"],
        "name": "OpenAI",
        "provider_type": "openai",
        "base_url": "https://1.1.1.1/v1",
        "has_secret": True,
        "secret_hint": "••••WXYZ",
        "enabled": True,
        "settings": {"timeout_seconds": 45},
        "last_test": None,
        "created_at": body["created_at"],
        "updated_at": body["updated_at"],
    }
    listed = await env.admin.get("/api/v1/providers")
    single = await env.admin.get(f"/api/v1/providers/{body['id']}")
    for text in (response.text, listed.text, single.text):
        assert OPENAI_KEY not in text
        assert OPENAI_KEY[:-4] not in text
    assert listed.json()["items"][0]["id"] == body["id"]

    providers = await env.harness.sql("SELECT row_to_json(p)::text FROM ai_providers p")
    secrets = await env.harness.sql("SELECT kind, hint, ciphertext FROM secret_references")
    assert OPENAI_KEY not in providers[0][0]
    assert len(secrets) == 1
    kind, hint, ciphertext = secrets[0]
    assert (kind, hint) == ("ai_api_key", "WXYZ")
    assert OPENAI_KEY.encode() not in bytes(ciphertext)
    audit = await env.harness.sql("SELECT details::text FROM audit_logs WHERE action = 'provider.created'")
    assert audit
    assert OPENAI_KEY not in audit[0][0]
    assert json.loads(audit[0][0]) == {"provider_type": "openai", "has_secret": True}


async def test_short_keys_have_no_hint(env: Env) -> None:
    body = await create(env.admin, **public_openai(api_key="short-key-1"))
    assert body["has_secret"] is True
    assert body["secret_hint"] is None


@pytest.mark.parametrize(
    ("override", "field", "code"),
    [
        ({"api_key": None}, "body.api_key", "secret_required"),
        ({"api_key": "  padded-key-with-spaces  "}, "body.api_key", "secret_invalid"),
        ({"name": "   "}, "body.name", "invalid_name"),
        ({"settings": {"timeout_seconds": 0}}, "body.settings.timeout_seconds", "greater_than_equal"),
        ({"settings": {"retries": 3}}, "body.settings.retries", "extra_forbidden"),
        ({"provider_type": "azure"}, "body.provider_type", "enum"),
        ({"organization_id": "x"}, "body.organization_id", "extra_forbidden"),
    ],
)
async def test_invalid_provider_input(env: Env, override: dict[str, Any], field: str, code: str) -> None:
    body = assert_problem(
        await env.admin.post("/api/v1/providers", json=public_openai(**override)), 422, "validation_error"
    )
    assert [(error["field"], error["code"]) for error in body["errors"]] == [(field, code)]
    assert "padded-key" not in json.dumps(body)


async def test_local_provider_needs_a_base_url_but_no_key(env: Env) -> None:
    missing = await env.admin.post("/api/v1/providers", json={"name": "Local", "provider_type": "ollama"})
    body = assert_problem(missing, 422, "validation_error")
    assert body["errors"][0]["code"] == "base_url_required"
    created = await create(env.admin, name="Local", provider_type="ollama", base_url=env.fake.url + "/")
    assert created["base_url"] == env.fake.url
    assert created["has_secret"] is False
    assert await env.harness.sql("SELECT count(*) FROM secret_references") == [(0,)]


@pytest.mark.parametrize(
    ("provider_type", "base_url", "reason"),
    [
        ("openai", "https://169.254.169.254/v1", "address_never_allowed"),  # cloud metadata
        (
            "openai_compatible",
            "http://10.0.0.5:8000/v1",
            "scheme_not_allowed",
        ),  # internal, no allowlist entry
        ("openai_compatible", "https://10.0.0.5/v1", "address_not_allowed"),
        ("openai_compatible", "https://localhost:8443/v1", "address_not_allowed"),  # resolves to loopback
        ("openai", "https://[::ffff:127.0.0.1]/v1", "address_not_allowed"),
        ("anthropic", "LOOPBACK", "scheme_not_allowed"),  # allowlisted address, but not for this type (SC-06)
    ],
)
async def test_endpoint_outside_the_network_policy_is_refused(
    env: Env, provider_type: str, base_url: str, reason: str
) -> None:
    """SC-02: an application administrator cannot reach internal addresses through a provider."""
    url = env.fake.url if base_url == "LOOPBACK" else base_url
    body = {"name": "Sneaky", "provider_type": provider_type, "base_url": url, "api_key": OPENAI_KEY}
    problem = assert_problem(
        await env.admin.post("/api/v1/providers", json=body), 422, "endpoint_not_allowed"
    )
    assert problem["errors"] == [
        {
            "field": "body.base_url",
            "code": reason,
            "message": "Endpoint is not allowed by instance network policy.",
        }
    ]
    assert await env.harness.sql("SELECT count(*) FROM ai_providers") == [(0,)]
    assert await env.harness.sql("SELECT count(*) FROM secret_references") == [(0,)]


@pytest.mark.parametrize(
    "base_url",
    [
        "ftp://example.com/v1",
        "https://user:pw@example.com/v1",
        "https://example.com/v1?key=x",
        "not a url",
        "https://example.com/a/../b",
    ],
)
async def test_malformed_base_url_is_invalid(env: Env, base_url: str) -> None:
    body = assert_problem(
        await env.admin.post("/api/v1/providers", json=public_openai(base_url=base_url)),
        422,
        "validation_error",
    )
    assert body["errors"][0]["code"] == "invalid_url"


async def test_provider_names_are_unique_case_insensitively(env: Env) -> None:
    await create(env.admin, **public_openai())
    assert_problem(
        await env.admin.post("/api/v1/providers", json=public_openai(name="openai")),
        409,
        "provider_name_taken",
    )


# --- update: api_key semantics --------------------------------------------------------------------------


async def test_patch_without_api_key_keeps_the_secret(env: Env) -> None:
    created = await create(env.admin, **public_openai())
    secret_before = await env.harness.sql("SELECT id, ciphertext FROM secret_references")
    response = await env.admin.patch(
        f"/api/v1/providers/{created['id']}", json={"name": "OpenAI main", "enabled": False}
    )
    assert response.status_code == 200, response.text
    assert (response.json()["name"], response.json()["enabled"]) == ("OpenAI main", False)
    assert response.json()["secret_hint"] == created["secret_hint"]
    assert await env.harness.sql("SELECT id, ciphertext FROM secret_references") == secret_before
    assert "provider.secret.rotated" not in await audit_actions(env.harness)


async def test_patch_with_api_key_replaces_it_and_resets_the_last_test(env: Env) -> None:
    created = await create(
        env.admin, name="Local", provider_type="openai_compatible", base_url=env.fake.url, api_key=OPENAI_KEY
    )
    env.fake.handler = lambda _: Reply.json({"data": [{"id": "m"}]})
    assert (await env.admin.post(f"/api/v1/providers/{created['id']}/test")).json()["status"] == "ok"
    (secret_id,) = (await env.harness.sql("SELECT id FROM secret_references"))[0]

    response = await env.admin.patch(f"/api/v1/providers/{created['id']}", json={"api_key": OTHER_KEY})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["secret_hint"] == "••••ABCD"
    assert body["last_test"] is None
    assert OTHER_KEY not in response.text
    assert await env.harness.sql("SELECT id, hint FROM secret_references") == [(secret_id, "ABCD")]
    rotated = await env.harness.sql(
        "SELECT details::text FROM audit_logs WHERE action = 'provider.secret.rotated'"
    )
    assert [json.loads(row[0]) for row in rotated] == [{"change": "replaced"}]

    # the new key is the one sent upstream
    await env.admin.post(f"/api/v1/providers/{created['id']}/test")
    assert env.fake.server.requests[-1].headers["authorization"] == f"Bearer {OTHER_KEY}"


async def test_patch_with_null_api_key_removes_it(env: Env) -> None:
    created = await create(
        env.admin, name="Local", provider_type="ollama", base_url=env.fake.url, api_key=OPENAI_KEY
    )
    response = await env.admin.patch(f"/api/v1/providers/{created['id']}", json={"api_key": None})
    assert response.status_code == 200, response.text
    assert (response.json()["has_secret"], response.json()["secret_hint"]) == (False, None)
    assert await env.harness.sql("SELECT count(*) FROM secret_references") == [(0,)]
    again = await env.admin.patch(f"/api/v1/providers/{created['id']}", json={"api_key": None})
    assert again.status_code == 200
    rotated = await env.harness.sql(
        "SELECT details::text FROM audit_logs WHERE action = 'provider.secret.rotated'"
    )
    assert [json.loads(row[0]) for row in rotated] == [{"change": "removed"}]


async def test_cloud_provider_key_cannot_be_removed_and_type_cannot_change(env: Env) -> None:
    created = await create(env.admin, **public_openai())
    url = f"/api/v1/providers/{created['id']}"
    body = assert_problem(await env.admin.patch(url, json={"api_key": None}), 422, "validation_error")
    assert body["errors"][0]["code"] == "secret_required"
    body = assert_problem(
        await env.admin.patch(url, json={"provider_type": "ollama"}), 422, "validation_error"
    )
    assert body["errors"][0]["code"] == "extra_forbidden"
    assert_problem(
        await env.admin.patch(url, json={"base_url": "https://169.254.169.254/"}), 422, "endpoint_not_allowed"
    )
    assert (await env.admin.get(url)).json()["base_url"] == "https://1.1.1.1/v1"


async def test_unknown_provider_is_404(env: Env) -> None:
    missing = "/api/v1/providers/00000000-0000-7000-8000-000000000000"
    assert_problem(await env.admin.get(missing), 404, "not_found")
    assert_problem(await env.admin.patch(missing, json={"name": "x"}), 404, "not_found")
    assert_problem(await env.admin.delete(missing), 404, "not_found")
    assert_problem(await env.admin.post(missing + "/test"), 404, "not_found")


# --- connection test ------------------------------------------------------------------------------------


async def test_connection_test_success_is_recorded_and_audited(env: Env) -> None:
    env.fake.handler = lambda _: Reply.json(
        {"models": [{"name": "llama3.2:3b"}, {"name": "nomic-embed-text"}]}
    )
    created = await create(env.admin, name="Ollama", provider_type="ollama", base_url=env.fake.url)
    response = await env.admin.post(f"/api/v1/providers/{created['id']}/test")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "ok"
    assert body["error"] is None
    assert body["models"] == ["llama3.2:3b", "nomic-embed-text"]
    assert env.fake.server.requests[-1].target == "/api/tags"
    last_test = (await env.admin.get(f"/api/v1/providers/{created['id']}")).json()["last_test"]
    assert last_test["status"] == "ok"
    assert last_test["error_code"] is None
    rows = await env.harness.sql(
        "SELECT outcome, details::text FROM audit_logs WHERE action = 'provider.tested'"
    )
    assert rows[0][0] == "success"
    assert json.loads(rows[0][1])["status"] == "ok"


async def test_connection_test_failure_returns_only_a_typed_code(env: Env) -> None:
    env.fake.handler = lambda _: Reply.json({"error": {"message": LEAK}}, 401)
    created = await create(
        env.admin,
        name="Compat",
        provider_type="openai_compatible",
        base_url=env.fake.url + "/v1",
        api_key=OPENAI_KEY,
    )
    response = await env.admin.post(f"/api/v1/providers/{created['id']}/test")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "failed"
    assert body["models"] == []
    assert body["error"] == {
        "code": "provider_auth_failed",
        "message": "The provider rejected the credentials.",
    }
    assert "upstream" not in response.text
    assert "req_123" not in response.text
    request = env.fake.server.requests[-1]
    assert (request.target, request.headers["authorization"]) == ("/v1/models", f"Bearer {OPENAI_KEY}")
    stored = await env.harness.sql("SELECT last_test_status, last_test_error FROM ai_providers")
    assert stored == [("failed", "provider_auth_failed")]
    rows = await env.harness.sql(
        "SELECT outcome, details::text FROM audit_logs WHERE action = 'provider.tested'"
    )
    assert rows[0][0] == "failure"
    assert "upstream" not in rows[0][1]
    assert OPENAI_KEY not in rows[0][1]


async def test_connection_test_is_rate_limited_per_installation(env: Env) -> None:
    created = await create(env.admin, name="Ollama", provider_type="ollama", base_url=env.fake.url)
    for _ in range(10):
        assert (await env.admin.post(f"/api/v1/providers/{created['id']}/test")).status_code == 200
    limited = await env.admin.post(f"/api/v1/providers/{created['id']}/test")
    assert_problem(limited, 429, "rate_limited")
    assert int(limited.headers["Retry-After"]) >= 1
    assert len(env.fake.server.requests) == 10


# --- delete ---------------------------------------------------------------------------------------------


async def test_delete_removes_key_and_models_and_frees_the_name(env: Env) -> None:
    created = await create(env.admin, **public_openai())
    url = f"/api/v1/providers/{created['id']}"
    assert (await env.admin.post(url + "/models", json={"model_id": "gpt-x"})).status_code == 201
    assert (await env.admin.delete(url)).status_code == 204
    assert_problem(await env.admin.get(url), 404, "not_found")
    assert_problem(await env.admin.get(url + "/models"), 404, "not_found")
    assert (await env.admin.get("/api/v1/providers")).json()["items"] == []
    assert await env.harness.sql("SELECT count(*) FROM secret_references") == [(0,)]
    assert await env.harness.sql("SELECT count(*) FROM model_configs") == [(0,)]
    assert await env.harness.sql(
        "SELECT secret_ref_id IS NULL, deleted_at IS NOT NULL FROM ai_providers"
    ) == [(True, True)]
    await create(env.admin, **public_openai())  # the name is free again


async def test_provider_used_by_an_embedding_profile_cannot_be_deleted(env: Env) -> None:
    created = await create(env.admin, name="Ollama", provider_type="ollama", base_url=env.fake.url)
    profile = await env.admin.post(
        "/api/v1/embedding-profiles",
        json={"name": "Local", "provider_id": created["id"], "model_id": "nomic-embed-text", "dimensions": 3},
    )
    assert profile.status_code == 201, profile.text
    assert_problem(await env.admin.delete(f"/api/v1/providers/{created['id']}"), 409, "provider_in_use")
    assert (await env.admin.get(f"/api/v1/providers/{created['id']}")).status_code == 200


# --- models ---------------------------------------------------------------------------------------------


async def test_model_configs_crud(env: Env) -> None:
    provider = await create(env.admin, **public_openai())
    other = await create(env.admin, **public_openai(name="Second"))
    base = f"/api/v1/providers/{provider['id']}/models"
    created = await env.admin.post(
        base,
        json={
            "model_id": "gpt-x",
            "display_name": "GPT X",
            "context_window": 128000,
            "max_output_tokens": 4096,
        },
    )
    assert created.status_code == 201, created.text
    model = created.json()
    assert (model["model_id"], model["kind"], model["enabled"]) == ("gpt-x", "chat", True)
    assert_problem(await env.admin.post(base, json={"model_id": "gpt-x"}), 409, "model_id_taken")
    assert (
        await env.admin.post(f"/api/v1/providers/{other['id']}/models", json={"model_id": "gpt-x"})
    ).status_code == 201

    body = assert_problem(await env.admin.post(base, json={"model_id": "gpt x"}), 422, "validation_error")
    assert body["errors"][0]["code"] == "invalid_model_id"
    item = f"{base}/{model['id']}"
    updated = await env.admin.patch(item, json={"display_name": None, "enabled": False})
    assert updated.status_code == 200, updated.text
    assert (updated.json()["display_name"], updated.json()["enabled"], updated.json()["context_window"]) == (
        None,
        False,
        128000,
    )
    body = assert_problem(await env.admin.patch(item, json={"model_id": "gpt-y"}), 422, "validation_error")
    assert body["errors"][0]["code"] == "extra_forbidden"
    # a model config is reachable only under its own provider
    assert_problem(
        await env.admin.patch(f"/api/v1/providers/{other['id']}/models/{model['id']}", json={}),
        404,
        "not_found",
    )
    listed = (await env.admin.get(base)).json()["items"]
    assert [entry["model_id"] for entry in listed] == ["gpt-x"]
    assert (await env.admin.delete(item)).status_code == 204
    assert (await env.admin.get(base)).json()["items"] == []
    actions = await audit_actions(env.harness)
    assert {"model.created", "model.updated", "model.deleted"} <= set(actions)


# --- embedding profiles ---------------------------------------------------------------------------------


async def test_embedding_profile_lifecycle(env: Env) -> None:
    provider = await create(env.admin, name="Ollama", provider_type="ollama", base_url=env.fake.url)
    response = await env.admin.post(
        "/api/v1/embedding-profiles",
        json={
            "name": "Local 768",
            "provider_id": provider["id"],
            "model_id": "nomic-embed-text",
            "dimensions": 3,
        },
    )
    assert response.status_code == 201, response.text
    profile = response.json()
    assert (profile["distance_metric"], profile["normalize"], profile["status"]) == (
        "cosine",
        False,
        "active",
    )
    url = f"/api/v1/embedding-profiles/{profile['id']}"

    assert_problem(
        await env.admin.post(
            "/api/v1/embedding-profiles",
            json={"name": "local 768", "provider_id": provider["id"], "model_id": "x", "dimensions": 3},
        ),
        409,
        "embedding_profile_name_taken",
    )
    for change in (
        {"dimensions": 1536},
        {"model_id": "other"},
        {"distance_metric": "l2"},
        {"normalize": True},
    ):
        assert_problem(await env.admin.patch(url, json=change), 409, "reindex_required")
    same = await env.admin.patch(url, json={"dimensions": 3, "name": "Local small"})
    assert same.status_code == 200, same.text
    assert same.json()["name"] == "Local small"
    assert (await env.admin.patch(url, json={"status": "retired"})).json()["status"] == "retired"
    assert_problem(await env.admin.patch(url, json={"status": "active"}), 409, "invalid_state")
    listed = (await env.admin.get("/api/v1/embedding-profiles")).json()
    assert [item["id"] for item in listed["items"]] == [profile["id"]]
    assert await env.harness.sql("SELECT dimensions, model_id FROM embedding_provider_configs") == [
        (3, "nomic-embed-text")
    ]


async def test_embedding_profile_needs_an_embedding_capable_provider(env: Env) -> None:
    anthropic = await create(
        env.admin, name="Claude", provider_type="anthropic", base_url="https://1.1.1.1", api_key=OPENAI_KEY
    )
    body = {"name": "P", "provider_id": anthropic["id"], "model_id": "m", "dimensions": 8}
    problem = assert_problem(
        await env.admin.post("/api/v1/embedding-profiles", json=body), 422, "validation_error"
    )
    assert problem["errors"][0]["code"] == "capability_not_supported"
    body["provider_id"] = "00000000-0000-7000-8000-000000000000"
    problem = assert_problem(
        await env.admin.post("/api/v1/embedding-profiles", json=body), 422, "validation_error"
    )
    assert problem["errors"][0]["code"] == "unknown_provider"


@pytest.mark.parametrize(
    ("dimensions", "status", "code"), [(3, "ok", None), (1536, "failed", "dimension_mismatch")]
)
async def test_embedding_profile_test_checks_the_dimension(
    env: Env, dimensions: int, status: str, code: str | None
) -> None:
    env.fake.handler = lambda request: Reply.json(
        {"embeddings": [[0.1, 0.2, 0.3] for _ in request.json()["input"]]}
    )
    provider = await create(env.admin, name="Ollama", provider_type="ollama", base_url=env.fake.url)
    profile = (
        await env.admin.post(
            "/api/v1/embedding-profiles",
            json={
                "name": "P",
                "provider_id": provider["id"],
                "model_id": "nomic-embed-text",
                "dimensions": dimensions,
            },
        )
    ).json()
    response = await env.admin.post(f"/api/v1/embedding-profiles/{profile['id']}/test")
    assert response.status_code == 200, response.text
    body = response.json()
    assert (body["status"], body["expected_dimensions"], body["actual_dimensions"]) == (status, dimensions, 3)
    assert (body["error"] or {}).get("code") == code
    request = env.fake.server.requests[-1]
    assert request.target == "/api/embed"
    assert request.json()["model"] == "nomic-embed-text"


async def test_embedding_profile_test_reports_provider_errors_safely(env: Env) -> None:
    env.fake.handler = lambda _: Reply.json({"error": LEAK}, 404)
    provider = await create(env.admin, name="Ollama", provider_type="ollama", base_url=env.fake.url)
    profile = (
        await env.admin.post(
            "/api/v1/embedding-profiles",
            json={"name": "P", "provider_id": provider["id"], "model_id": "absent", "dimensions": 3},
        )
    ).json()
    response = await env.admin.post(f"/api/v1/embedding-profiles/{profile['id']}/test")
    body = response.json()
    assert body["status"] == "failed"
    assert body["error"]["code"] == "provider_model_not_found"
    assert "upstream" not in response.text


# --- permissions ----------------------------------------------------------------------------------------


async def test_permissions_on_providers_and_profiles(env: Env) -> None:
    provider = await create(env.admin, name="Ollama", provider_type="ollama", base_url=env.fake.url)
    profile = (
        await env.admin.post(
            "/api/v1/embedding-profiles",
            json={"name": "P", "provider_id": provider["id"], "model_id": "m", "dimensions": 3},
        )
    ).json()
    await active_user(env.harness, env.admin, "km@example.com", ["knowledge_manager"])
    await active_user(env.harness, env.admin, "member@example.com", ["member"])
    p_url, e_url = f"/api/v1/providers/{provider['id']}", f"/api/v1/embedding-profiles/{profile['id']}"

    async with env.harness.new_client() as manager:
        assert (await login(manager, "km@example.com")).status_code == 200
        for path in ("/api/v1/providers", p_url, p_url + "/models", "/api/v1/embedding-profiles", e_url):
            assert (await manager.get(path)).status_code == 200, path
        denied = [
            await manager.post("/api/v1/providers", json=public_openai()),
            await manager.patch(p_url, json={"name": "x"}),
            await manager.delete(p_url),
            await manager.post(p_url + "/test"),
            await manager.post(p_url + "/models", json={"model_id": "m"}),
            await manager.post(
                "/api/v1/embedding-profiles",
                json={"name": "Q", "provider_id": provider["id"], "model_id": "m", "dimensions": 3},
            ),
            await manager.patch(e_url, json={"name": "Q"}),
            await manager.post(e_url + "/test"),
        ]
        assert [response.status_code for response in denied] == [403] * len(denied)

    async with env.harness.new_client() as member:
        assert (await login(member, "member@example.com")).status_code == 200
        for path in ("/api/v1/providers", p_url, p_url + "/models", "/api/v1/embedding-profiles", e_url):
            assert_problem(await member.get(path), 403, "permission_denied")

    async with env.harness.new_client() as anonymous:
        assert_problem(await anonymous.get("/api/v1/providers"), 401, "unauthenticated")
        assert_problem(
            await anonymous.post("/api/v1/providers", json=public_openai()), 401, "unauthenticated"
        )
    assert len(env.fake.server.requests) == 0


async def test_without_a_key_ring_keys_cannot_be_stored(
    migrated_database_url: str, redis_url: str, clean_rate_limits: None, tmp_path: Path
) -> None:
    async with running_auth_app(
        migrated_database_url, redis_url, tmp_path / "bootstrap", secrets_keyring_file=None
    ) as harness:
        admin = await setup_admin_client(harness)
        assert_problem(
            await admin.post("/api/v1/providers", json=public_openai()), 503, "secrets_not_configured"
        )
        assert await harness.sql("SELECT count(*) FROM ai_providers") == [(0,)]
