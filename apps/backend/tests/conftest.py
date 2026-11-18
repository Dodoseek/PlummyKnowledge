from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from plummy.api.dependencies import get_readiness
from plummy.application.health import CheckReadiness
from plummy.core.config import Settings, get_settings, load_settings

CONFIG_ENV_VARS = (
    "APP_ENV",
    "APP_DEBUG",
    "LOG_LEVEL",
    "SECRET_KEY",
    "SECRET_KEY_FILE",
    "DATABASE_URL",
    "DATABASE_URL_FILE",
    "REDIS_URL",
    "REDIS_URL_FILE",
    "CORS_ORIGINS",
    "DB_POOL_SIZE",
    "DB_MAX_OVERFLOW",
    "API_DOCS_ENABLED",
    "PUBLIC_URL",
    "SETUP_MODE",
    "BOOTSTRAP_DIR",
    "BOOTSTRAP_TOKEN_TTL",
    "SESSION_IDLE_TTL",
    "SESSION_ABSOLUTE_TTL",
    "SESSION_COOKIE_SECURE",
)

DB_PASSWORD = "db-password-should-never-leak"
VALID_DATABASE_URL = f"postgresql+asyncpg://plummy:{DB_PASSWORD}@db.internal:5432/plummy"
VALID_REDIS_URL = "redis://redis.internal:6379/0"
VALID_SECRET_KEY = "s" * 40
# PUBLIC_URL is required in production (ADR-013); the test client's origin.
TEST_PUBLIC_URL = "https://testserver"


@pytest.fixture(autouse=True)
def _keep_pytest_log_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    """create_app() reconfigures root logging (JSON to stdout), which would detach pytest's caplog handler."""
    monkeypatch.setattr("plummy.main.configure_logging", lambda level: None)


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> Iterator[pytest.MonkeyPatch]:
    """Unit tests must not depend on the developer's or container's environment."""
    for name in CONFIG_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


def make_settings(**overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "app_env": "test",
        "database_url": VALID_DATABASE_URL,
        "redis_url": VALID_REDIS_URL,
        "public_url": TEST_PUBLIC_URL,
    }
    values.update(overrides)
    return load_settings(**values)


class FakeCheck:
    def __init__(self, name: str, error: BaseException | None = None, delay: float = 0.0) -> None:
        self.name = name
        self._error = error
        self._delay = delay
        self.calls = 0

    async def check(self) -> None:
        import asyncio

        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._error is not None:
            raise self._error


def build_test_app(
    checks: list[FakeCheck] | None = None, **settings_overrides: Any
) -> tuple[FastAPI, CheckReadiness]:
    """App without lifespan resources: readiness is replaced with fake dependency checks."""
    from plummy.main import create_app

    app = create_app(make_settings(**settings_overrides))
    readiness = CheckReadiness(checks if checks is not None else [FakeCheck("database"), FakeCheck("redis")])
    app.dependency_overrides[get_readiness] = lambda: readiness
    return app, readiness


@pytest.fixture
async def client(clean_env: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    app, _ = build_test_app()
    async with make_client(app) as http_client:
        yield http_client


def make_client(app: FastAPI) -> httpx.AsyncClient:
    # raise_app_exceptions=False: the server-error handler response is returned like in production.
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")
