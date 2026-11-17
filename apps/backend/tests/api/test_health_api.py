from __future__ import annotations

import httpx
import pytest

from tests.conftest import DB_PASSWORD, VALID_SECRET_KEY, FakeCheck, build_test_app, make_client

pytestmark = pytest.mark.usefixtures("clean_env")


async def test_live_returns_ok(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/health/live")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["cache-control"] == "no-store"


async def test_live_does_not_depend_on_dependencies() -> None:
    database = FakeCheck("database", error=ConnectionRefusedError())
    app, _ = build_test_app([database])
    async with make_client(app) as client:
        response = await client.get("/api/v1/health/live")
    assert response.status_code == 200
    assert database.calls == 0


async def test_ready_returns_minimal_body_when_all_dependencies_are_up(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}
    assert response.headers["cache-control"] == "no-store"


async def test_ready_returns_503_with_check_statuses_when_a_dependency_is_down() -> None:
    error = OSError(f"connect to postgresql://plummy:{DB_PASSWORD}@10.0.0.5:5432 failed")
    app, _ = build_test_app([FakeCheck("database", error=error), FakeCheck("migrations"), FakeCheck("redis")])
    async with make_client(app) as client:
        response = await client.get("/api/v1/health/ready")

    assert response.status_code == 503
    assert response.json() == {
        "status": "not_ready",
        "checks": {"database": "fail", "migrations": "ok", "redis": "ok"},
    }
    # No hosts, credentials or error texts in the public response.
    assert DB_PASSWORD not in response.text
    assert "10.0.0.5" not in response.text
    assert "OSError" not in response.text


async def test_ready_recovers_when_dependency_comes_back() -> None:
    redis = FakeCheck("redis", error=ConnectionError())
    app, _ = build_test_app([redis])
    async with make_client(app) as client:
        assert (await client.get("/api/v1/health/ready")).status_code == 503
        redis._error = None
        assert (await client.get("/api/v1/health/ready")).status_code == 200


@pytest.mark.parametrize("method", ["post", "put", "delete"])
async def test_health_endpoints_reject_unsafe_methods(client: httpx.AsyncClient, method: str) -> None:
    response = await client.request(method.upper(), "/api/v1/health/ready")
    assert response.status_code == 405
    assert response.headers["content-type"] == "application/problem+json"
    assert response.json()["code"] == "method_not_allowed"


async def test_openapi_schema_is_served_under_api_prefix(client: httpx.AsyncClient) -> None:
    response = await client.get("/api/v1/openapi.json")
    assert response.status_code == 200
    schema = response.json()
    assert schema["info"]["title"] == "PlummyKnowledge API"
    assert {"/api/v1/health/live", "/api/v1/health/ready"} <= set(schema["paths"])


@pytest.mark.parametrize(
    ("app_env", "api_docs_enabled", "docs_status"),
    [
        ("development", None, 200),
        ("test", None, 200),
        ("production", None, 404),  # Swagger UI loads assets from a public CDN: off by default
        ("production", True, 200),  # explicit opt-in by the operator
        ("development", False, 404),
    ],
)
async def test_swagger_ui_is_disabled_in_production_but_schema_is_served(
    app_env: str, api_docs_enabled: bool | None, docs_status: int
) -> None:
    app, _ = build_test_app(app_env=app_env, secret_key=VALID_SECRET_KEY, api_docs_enabled=api_docs_enabled)
    async with make_client(app) as client:
        docs = await client.get("/api/v1/docs")
        schema = await client.get("/api/v1/openapi.json")
    assert docs.status_code == docs_status
    if docs_status == 404:
        assert docs.headers["content-type"] == "application/problem+json"
    else:
        assert "swagger-ui" in docs.text
    assert schema.status_code == 200
    assert "/api/v1/docs" not in schema.json()["paths"]


async def test_unversioned_paths_are_not_routed(client: httpx.AsyncClient) -> None:
    assert (await client.get("/health/live")).status_code == 404
    assert (await client.get("/api/health/live")).status_code == 404
