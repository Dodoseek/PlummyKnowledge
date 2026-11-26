"""Running app + HTTP client against real PostgreSQL and Redis for setup/auth integration tests."""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from redis import Redis
from redis.exceptions import RedisError
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from plummy.composition import Resources, build_cli_services, build_resources
from plummy.core.config import Settings
from plummy.infrastructure.security.passwords import Argon2PasswordHasher
from plummy.infrastructure.security.secret_cipher import create_keyring_file
from plummy.main import create_app
from tests.conftest import make_settings
from tests.integration.conftest import FAST_HASHER_PARAMS, TEST_ORIGIN, runtime_url

ADMIN_EMAIL = "admin@example.com"
ADMIN_PASSWORD = "correct horse battery staple"
ADMIN_NAME = "First Admin"


def fast_hasher() -> Argon2PasswordHasher:
    return Argon2PasswordHasher(**FAST_HASHER_PARAMS)


def make_keyring(directory: Path, key_id: str = "test-1") -> Path:
    """A real key ring with a fresh random key (ADR-016: tests generate the key in a fixture)."""
    path = directory / "keyring.json"
    create_keyring_file(path, key_id)
    return path


def _storage_root(base: Path) -> Path:
    root = base / "storage"
    root.mkdir(mode=0o700, exist_ok=True)
    return root


def harness_settings(database_url: str, redis_url: str, bootstrap_dir: Path, **overrides: Any) -> Settings:
    """``database_url`` is the owner's (migrations); the app connects as the runtime role like a service."""
    values: dict[str, Any] = {
        "database_url": runtime_url(database_url),
        "redis_url": redis_url,
        "public_url": TEST_ORIGIN,
        "bootstrap_dir": bootstrap_dir,
        # Production-like cookie (``__Host-``); explicit so the dev compose value (false) does not leak in.
        "session_cookie_secure": True,
        "secrets_keyring_file": make_keyring(bootstrap_dir.parent),
        "storage_path": _storage_root(bootstrap_dir.parent),
    }
    values.update(overrides)
    return make_settings(**values)


@dataclass
class Harness:
    app: FastAPI
    client: httpx.AsyncClient
    settings: Settings
    # The schema owner's URL (ADR-024): test data and inspection queries, never the application's own access.
    owner_url: str
    # Celery key prefix of this app: a dev worker on the same Redis never sees the test's messages.
    job_namespace: str = ""

    def queued_messages(self, queue: str) -> list[dict[str, Any]]:
        """Messages waiting in a Celery queue of this harness (kombu's Redis list)."""
        client = Redis.from_url(self.settings.redis_url.get_secret_value())
        try:
            return [json.loads(raw) for raw in client.lrange(f"{self.job_namespace}{queue}", 0, -1)]
        finally:
            client.close()

    @property
    def resources(self) -> Resources:
        resources: Resources = self.app.state.resources
        return resources

    def new_client(self, **kwargs: Any) -> httpx.AsyncClient:
        """Another browser: own cookie jar."""
        return make_https_client(self.app, **kwargs)

    async def issue_bootstrap_token(self, *, rotate: bool = False) -> str:
        services = build_cli_services(self.settings, password_hasher=fast_hasher())
        try:
            await services.issue_bootstrap_token.execute(rotate=rotate)
        finally:
            await services.aclose()
        return (self.settings.bootstrap_dir / "setup-token").read_text(encoding="ascii").strip()

    async def initialize(
        self,
        token: str,
        *,
        client: httpx.AsyncClient | None = None,
        email: str = ADMIN_EMAIL,
        password: str = ADMIN_PASSWORD,
        display_name: str = ADMIN_NAME,
    ) -> httpx.Response:
        return await (client or self.client).post(
            "/api/v1/setup/initialize",
            json={
                "bootstrap_token": token,
                "admin": {"email": email, "display_name": display_name, "password": password},
            },
        )

    async def setup_admin(self) -> httpx.Response:
        response = await self.initialize(await self.issue_bootstrap_token())
        assert response.status_code == 201, response.text
        return response

    async def create_user(self, email: str, password: str, role: str | None) -> str:
        """A user created directly in the database (user management API is a later stage)."""
        from plummy.core.ids import new_id

        user_id = str(new_id())
        password_hash = await fast_hasher().hash(password)
        await self.sql(
            "INSERT INTO users (id, email, display_name, password_hash, password_changed_at) "
            "VALUES (CAST(:id AS uuid), :email, 'User', :hash, now() - interval '1 minute')",
            id=user_id,
            email=email,
            hash=password_hash,
        )
        if role is not None:
            await self.sql(
                "INSERT INTO user_roles (user_id, role_id) "
                "SELECT CAST(:id AS uuid), id FROM roles WHERE key = :role",
                id=user_id,
                role=role,
            )
        return user_id

    async def sql(self, statement: str, **params: Any) -> list[Any]:
        engine = create_async_engine(self.owner_url)
        try:
            async with engine.begin() as connection:
                result = await connection.execute(text(statement), params)
                return list(result.all()) if result.returns_rows else []
        finally:
            await engine.dispose()


def make_https_client(app: FastAPI, **kwargs: Any) -> httpx.AsyncClient:
    headers = {"Origin": TEST_ORIGIN, **kwargs.pop("headers", {})}
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url=TEST_ORIGIN, headers=headers, **kwargs)


@asynccontextmanager
async def running_auth_app(
    database_url: str, redis_url: str, bootstrap_dir: Path, **overrides: Any
) -> AsyncIterator[Harness]:
    settings = harness_settings(database_url, redis_url, bootstrap_dir, **overrides)
    namespace = f"plummy:test:{uuid.uuid4().hex[:12]}:"
    app = create_app(
        settings,
        resources_factory=lambda s: build_resources(
            s, password_hasher=fast_hasher(), job_namespace=namespace
        ),
    )
    try:
        async with app.router.lifespan_context(app), make_https_client(app) as client:
            yield Harness(
                app=app, client=client, settings=settings, owner_url=database_url, job_namespace=namespace
            )
    finally:
        redis = Redis.from_url(redis_url)
        try:
            # Tests of an unreachable Redis have nothing to clean up.
            with suppress(RedisError):
                keys = list(redis.scan_iter(match=f"{namespace}*"))
                if keys:
                    redis.delete(*keys)
        finally:
            redis.close()
