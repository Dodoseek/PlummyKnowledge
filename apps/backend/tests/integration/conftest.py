"""Integration fixtures: a throw-away PostgreSQL database per test and the real Redis.

Run inside the dev stack: ``docker compose exec backend pytest``. Databases are created and migrated by the
schema owner role (TEST_OWNER_DATABASE_URL, else DATABASE_URL) — not a superuser (ADR-011). In development
that role has CREATEDB, and the required extensions are pre-installed in ``template1`` by the postgres init
script, so a new database looks like one prepared by a DBA; ``template0`` gives a database without
extensions. When DATABASE_URL is another role, it is the runtime role of the services (ADR-024): the migrated
database grants it its privileges and the application under test connects with it.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import make_url, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from plummy.core.config import get_settings
from plummy.core.version import BACKEND_ROOT

if TYPE_CHECKING:
    from tests.integration.harness import Harness


# Read once: the Migrator points DATABASE_URL at a test database while a test runs.
_OWNER_URL = os.environ.get("TEST_OWNER_DATABASE_URL")
_RUNTIME_URL = os.environ.get("DATABASE_URL")


def _base_url() -> str:
    url = _OWNER_URL or _RUNTIME_URL
    if not url or not os.environ.get("REDIS_URL"):
        pytest.skip("integration tests need DATABASE_URL and REDIS_URL (run them via docker compose)")
    return url


def runtime_role() -> str | None:
    """The runtime role of the environment, or ``None`` when the tests run with one role only."""
    if not _RUNTIME_URL or not _OWNER_URL:
        return None
    role = make_url(_RUNTIME_URL).username
    return role if role != make_url(_OWNER_URL).username else None


def runtime_url(owner_url: str) -> str:
    """The same database as ``owner_url``, through the runtime role when there is one."""
    if runtime_role() is None or _RUNTIME_URL is None:
        return owner_url
    runtime = make_url(_RUNTIME_URL)
    url = make_url(owner_url).set(username=runtime.username, password=runtime.password)
    return url.render_as_string(hide_password=False)


async def _drop_database(admin: AsyncEngine, name: str, attempts: int = 5) -> None:
    """``WITH (FORCE)`` cannot terminate backends of other roles (e.g. an autovacuum worker that just
    started on the database); those finish quickly, so retry briefly instead of failing the test."""
    for attempt in range(attempts):
        try:
            async with admin.connect() as connection:
                await connection.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
            return
        except DBAPIError as exc:
            if "permission denied to terminate process" not in str(exc) or attempt == attempts - 1:
                raise
            await asyncio.sleep(0.2 * (attempt + 1))


@asynccontextmanager
async def _temporary_database(template: str) -> AsyncIterator[str]:
    base_url = make_url(_base_url())
    name = f"plummy_test_{uuid.uuid4().hex[:12]}"
    admin = create_async_engine(base_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as connection:
            await connection.execute(text(f'CREATE DATABASE "{name}" TEMPLATE "{template}"'))
        yield base_url.set(database=name).render_as_string(hide_password=False)
        await _drop_database(admin, name)
    finally:
        await admin.dispose()


@pytest.fixture
async def database_url() -> AsyncIterator[str]:
    """A database prepared like production: extensions installed by the administrator, no tables."""
    async with _temporary_database("template1") as url:
        yield url


@pytest.fixture
async def bare_database_url() -> AsyncIterator[str]:
    """A database where the administrator has NOT installed the required extensions."""
    async with _temporary_database("template0") as url:
        yield url


@pytest.fixture
def redis_url() -> str:
    _base_url()
    return os.environ["REDIS_URL"]


class Migrator:
    """Runs Alembic exactly like the ``migrate`` service does, against the given database."""

    def __init__(self, database_url: str, monkeypatch: pytest.MonkeyPatch) -> None:
        self._config = Config(str(BACKEND_ROOT / "alembic.ini"))
        self._database_url = database_url
        self._monkeypatch = monkeypatch

    async def _run(self, action: str, revision: str) -> None:
        self._monkeypatch.setenv("DATABASE_URL", self._database_url)
        self._monkeypatch.delenv("DATABASE_URL_FILE", raising=False)
        get_settings.cache_clear()
        try:
            # env.py calls asyncio.run(); run it in a thread that has no event loop.
            await asyncio.to_thread(getattr(command, action), self._config, revision)
        finally:
            get_settings.cache_clear()

    async def upgrade(self, revision: str = "head") -> None:
        await self._run("upgrade", revision)

    async def downgrade(self, revision: str = "base") -> None:
        await self._run("downgrade", revision)


@pytest.fixture
def migrator(database_url: str, monkeypatch: pytest.MonkeyPatch) -> Migrator:
    return Migrator(database_url, monkeypatch)


# --- setup / auth harness --------------------------------------------------------------------------------

# Real Argon2id with minimal cost so the suite stays fast; production parameters are covered by unit tests.
FAST_HASHER_PARAMS = {"time_cost": 1, "memory_cost_kib": 8, "parallelism": 1}
TEST_ORIGIN = "https://testserver"


async def grant_runtime_privileges(owner_url: str) -> None:
    """What ``migrate`` does after the upgrade: ``python -m plummy.cli db grant-runtime`` (ADR-024 §2)."""
    from plummy.infrastructure.db.roles import grant_runtime

    role = runtime_role()
    if role is None:
        return
    engine = create_async_engine(owner_url)
    try:
        await grant_runtime(engine, role)
    finally:
        await engine.dispose()


@pytest.fixture
async def migrated_database_url(migrator: Migrator, database_url: str) -> str:
    """Migrated by the owner, privileges granted to the runtime role; the URL is the owner's."""
    await migrator.upgrade()
    await grant_runtime_privileges(database_url)
    return database_url


@pytest.fixture
async def clean_rate_limits(redis_url: str) -> AsyncIterator[None]:
    """Rate-limit counters are shared Redis state: start and finish every test with none."""
    from redis.asyncio import Redis

    from plummy.infrastructure.redis.rate_limiter import KEY_PREFIX

    client = Redis.from_url(redis_url, decode_responses=True)

    async def flush() -> None:
        keys = [key async for key in client.scan_iter(match=f"{KEY_PREFIX}*")]
        if keys:
            await client.delete(*keys)

    try:
        await flush()
        yield
        await flush()
    finally:
        await client.aclose()


@pytest.fixture
async def harness(
    migrated_database_url: str, redis_url: str, clean_rate_limits: None, tmp_path: Path
) -> AsyncIterator[Harness]:
    from tests.integration.harness import running_auth_app

    async with running_auth_app(migrated_database_url, redis_url, tmp_path / "bootstrap") as running:
        yield running
