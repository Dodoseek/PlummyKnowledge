from __future__ import annotations

import logging
import re

import pytest
from fastapi import FastAPI, HTTPException, Query

from plummy.core.config import get_settings
from tests.conftest import DB_PASSWORD, build_test_app, make_client

pytestmark = pytest.mark.usefixtures("clean_env")

SECRET_IN_ERROR = f"connection to postgresql://plummy:{DB_PASSWORD}@db failed"


def _app_with_test_routes(**settings_overrides: object) -> FastAPI:
    app, _ = build_test_app(**settings_overrides)

    @app.get("/api/v1/_test/boom")
    async def boom() -> None:
        raise RuntimeError(SECRET_IN_ERROR)

    @app.get("/api/v1/_test/items")
    async def items(limit: int = Query(ge=1, le=100)) -> dict[str, int]:
        return {"limit": limit}

    @app.get("/api/v1/_test/conflict")
    async def conflict() -> None:
        raise HTTPException(status_code=409, detail="Knowledge base name already exists.")

    @app.get("/api/v1/_test/teapot")
    async def teapot() -> None:
        raise HTTPException(status_code=418, detail="Short and stout.")

    @app.get("/api/v1/_test/upstream")
    async def upstream() -> None:
        raise HTTPException(status_code=502, detail=SECRET_IN_ERROR)

    return app


# --- error format ---------------------------------------------------------------------------------------


async def test_unknown_route_returns_problem_json() -> None:
    async with make_client(_app_with_test_routes()) as client:
        response = await client.get("/api/v1/does-not-exist")
    assert response.status_code == 404
    assert response.headers["content-type"] == "application/problem+json"
    body = response.json()
    assert body["code"] == "not_found"
    assert body["status"] == 404
    assert body["title"] == "Not Found"
    assert body["correlation_id"] == response.headers["x-request-id"]
    assert "detail" not in body  # default phrase is not duplicated


async def test_http_exception_detail_is_preserved_for_client_errors() -> None:
    async with make_client(_app_with_test_routes()) as client:
        response = await client.get("/api/v1/_test/conflict")
    assert response.status_code == 409
    assert response.json()["code"] == "conflict"
    assert response.json()["detail"] == "Knowledge base name already exists."


async def test_validation_error_lists_fields_without_echoing_input() -> None:
    async with make_client(_app_with_test_routes()) as client:
        response = await client.get("/api/v1/_test/items", params={"limit": "secret-token-value"})
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert body["errors"] == [
        {"field": "query.limit", "code": "int_parsing", "message": body["errors"][0]["message"]}
    ]
    assert "secret-token-value" not in response.text


async def test_validation_error_for_missing_parameter() -> None:
    async with make_client(_app_with_test_routes()) as client:
        response = await client.get("/api/v1/_test/items")
    assert response.status_code == 422
    assert response.json()["errors"][0]["code"] == "missing"


async def test_unhandled_exception_returns_generic_500(caplog: pytest.LogCaptureFixture) -> None:
    async with make_client(_app_with_test_routes()) as client:
        with caplog.at_level(logging.ERROR, logger="plummy.api.errors"):
            response = await client.get("/api/v1/_test/boom", headers={"X-Request-ID": "trace-500"})

    assert response.status_code == 500
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["x-request-id"] == "trace-500"
    body = response.json()
    assert body == {
        "type": "about:blank",
        "title": "Internal Server Error",
        "status": 500,
        "code": "internal_error",
        "correlation_id": "trace-500",
    }
    assert DB_PASSWORD not in response.text
    assert "Traceback" not in response.text
    # The server log keeps the diagnostics, correlated by request ID.
    record = next(r for r in caplog.records if r.getMessage() == "unhandled exception")
    assert record.__dict__["request_id"] == "trace-500"
    assert record.exc_info is not None


async def test_wrong_method_returns_405_with_allow_header() -> None:
    async with make_client(_app_with_test_routes()) as client:
        response = await client.post("/api/v1/health/live")
    assert response.status_code == 405
    assert response.headers["content-type"] == "application/problem+json"
    assert response.headers["allow"] == "GET"
    assert response.json()["code"] == "method_not_allowed"
    assert response.json()["correlation_id"] == response.headers["x-request-id"]


async def test_unmapped_client_error_status_falls_back_to_bad_request() -> None:
    async with make_client(_app_with_test_routes()) as client:
        response = await client.get("/api/v1/_test/teapot")
    assert response.status_code == 418
    assert response.json()["code"] == "bad_request"
    assert response.json()["detail"] == "Short and stout."


async def test_server_error_http_exception_hides_detail() -> None:
    async with make_client(_app_with_test_routes()) as client:
        response = await client.get("/api/v1/_test/upstream")
    assert response.status_code == 502
    body = response.json()
    assert body["code"] == "internal_error"
    assert "detail" not in body
    assert DB_PASSWORD not in response.text


# --- request ID -----------------------------------------------------------------------------------------


async def test_request_id_is_generated_when_missing() -> None:
    async with make_client(_app_with_test_routes()) as client:
        first = await client.get("/api/v1/health/live")
        second = await client.get("/api/v1/health/live")
    assert re.fullmatch(r"[0-9a-f]{32}", first.headers["x-request-id"])
    assert first.headers["x-request-id"] != second.headers["x-request-id"]


async def test_valid_client_request_id_is_echoed() -> None:
    async with make_client(_app_with_test_routes()) as client:
        response = await client.get("/api/v1/health/live", headers={"X-Request-ID": "abc.DEF_123-x"})
    assert response.headers["x-request-id"] == "abc.DEF_123-x"


@pytest.mark.parametrize("bad_id", ["has space", "a" * 129, "line\\nbreak", "<script>", "ид"])
async def test_unsafe_client_request_id_is_replaced(bad_id: str) -> None:
    async with make_client(_app_with_test_routes()) as client:
        response = await client.get("/api/v1/health/live", headers=[("X-Request-ID", bad_id.encode("utf-8"))])
    assert response.headers["x-request-id"] != bad_id
    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["x-request-id"])


async def test_access_log_has_path_but_not_query_string(caplog: pytest.LogCaptureFixture) -> None:
    async with make_client(_app_with_test_routes()) as client:
        with caplog.at_level(logging.INFO, logger="plummy.access"):
            await client.get("/api/v1/health/live", params={"token": "oauth-code-123"})
    record = next(r for r in caplog.records if r.name == "plummy.access")
    assert record.__dict__["path"] == "/api/v1/health/live"
    assert record.__dict__["status"] == 200
    assert "oauth-code-123" not in caplog.text


# --- CORS -----------------------------------------------------------------------------------------------


async def test_cors_is_closed_by_default() -> None:
    async with make_client(_app_with_test_routes()) as client:
        response = await client.get("/api/v1/health/live", headers={"Origin": "https://evil.example"})
    assert "access-control-allow-origin" not in response.headers


async def test_cors_allows_only_configured_origins() -> None:
    app = _app_with_test_routes(cors_origins="https://kb.example.com")
    preflight_headers = {"Access-Control-Request-Method": "POST"}
    async with make_client(app) as client:
        allowed = await client.options(
            "/api/v1/health/live", headers={"Origin": "https://kb.example.com", **preflight_headers}
        )
        denied = await client.options(
            "/api/v1/health/live", headers={"Origin": "https://evil.example", **preflight_headers}
        )
    assert allowed.status_code == 200
    assert allowed.headers["access-control-allow-origin"] == "https://kb.example.com"
    assert allowed.headers["access-control-allow-credentials"] == "true"
    assert "x-request-id" in allowed.headers
    assert denied.status_code == 400
    assert "access-control-allow-origin" not in denied.headers


# --- startup --------------------------------------------------------------------------------------------


def test_invalid_configuration_aborts_startup_without_leaking_secrets(
    clean_env: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from plummy.main import create_app

    clean_env.setenv("APP_ENV", "production")
    clean_env.setenv("DATABASE_URL", f"postgresql+asyncpg://plummy:{DB_PASSWORD}@db/plummy")
    clean_env.setenv("REDIS_URL", "redis://redis:6379/0")
    get_settings.cache_clear()

    with caplog.at_level(logging.CRITICAL, logger="plummy"), pytest.raises(SystemExit) as exc_info:
        create_app()

    assert exc_info.value.code == 1
    assert "startup aborted" in caplog.text
    assert "SECRET_KEY" in caplog.text
    assert DB_PASSWORD not in caplog.text


def test_check_configuration_returns_zero_for_valid_configuration(clean_env: pytest.MonkeyPatch) -> None:
    from plummy.main import check_configuration

    clean_env.setenv("APP_ENV", "development")
    clean_env.setenv("DATABASE_URL", f"postgresql+asyncpg://plummy:{DB_PASSWORD}@db/plummy")
    clean_env.setenv("REDIS_URL", "redis://redis:6379/0")

    assert check_configuration() == 0


def test_check_configuration_returns_one_without_leaking_secrets(
    clean_env: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from plummy.main import check_configuration

    clean_env.setenv("APP_ENV", "development")
    clean_env.setenv("DATABASE_URL", f"mysql://plummy:{DB_PASSWORD}@db/plummy")
    clean_env.setenv("REDIS_URL", "redis://redis:6379/0")

    with caplog.at_level(logging.CRITICAL, logger="plummy"):
        assert check_configuration() == 1

    assert "DATABASE_URL" in caplog.text
    assert DB_PASSWORD not in caplog.text
