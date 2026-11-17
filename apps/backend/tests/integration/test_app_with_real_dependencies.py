from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import text

from plummy.api.dependencies import get_db_session
from plummy.composition import Resources
from plummy.main import create_app
from tests.conftest import make_client, make_settings
from tests.integration.conftest import Migrator

pytestmark = pytest.mark.integration

UNREACHABLE_REDIS = "redis://127.0.0.1:1/0"
UNREACHABLE_DATABASE = "postgresql+asyncpg://plummy:x@127.0.0.1:1/plummy"


@asynccontextmanager
async def running_app(database_url: str, redis_url: str) -> AsyncIterator[tuple[FastAPI, httpx.AsyncClient]]:
    app = create_app(make_settings(database_url=database_url, redis_url=redis_url))
    async with app.router.lifespan_context(app), make_client(app) as client:
        yield app, client


async def test_ready_when_database_migrated_and_redis_available(
    migrator: Migrator, database_url: str, redis_url: str
) -> None:
    await migrator.upgrade()
    async with running_app(database_url, redis_url) as (_, client):
        response = await client.get("/api/v1/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


async def test_not_ready_until_migrations_are_applied(
    migrator: Migrator, database_url: str, redis_url: str
) -> None:
    async with running_app(database_url, redis_url) as (_, client):
        before = await client.get("/api/v1/health/ready")
        await migrator.upgrade()
        after = await client.get("/api/v1/health/ready")

    assert before.status_code == 503
    assert before.json()["checks"] == {"database": "ok", "migrations": "fail", "redis": "ok"}
    assert after.status_code == 200


async def test_not_ready_when_redis_is_unreachable(migrator: Migrator, database_url: str) -> None:
    await migrator.upgrade()
    async with running_app(database_url, UNREACHABLE_REDIS) as (_, client):
        live = await client.get("/api/v1/health/live")
        ready = await client.get("/api/v1/health/ready")
    assert live.status_code == 200  # the process itself is fine
    assert ready.status_code == 503
    assert ready.json()["checks"] == {"database": "ok", "migrations": "ok", "redis": "fail"}
    assert "127.0.0.1" not in ready.text


async def test_not_ready_when_database_is_unreachable(redis_url: str) -> None:
    async with running_app(UNREACHABLE_DATABASE, redis_url) as (_, client):
        ready = await client.get("/api/v1/health/ready")
    assert ready.status_code == 503
    assert ready.json()["checks"] == {"database": "fail", "migrations": "fail", "redis": "ok"}


async def test_session_dependency_rolls_back_uncommitted_work(
    migrator: Migrator, database_url: str, redis_url: str
) -> None:
    await migrator.upgrade()
    async with running_app(database_url, redis_url) as (app, _):
        resources: Resources = app.state.resources
        async with resources.engine.begin() as connection:
            await connection.execute(text("CREATE TABLE probe (id int PRIMARY KEY)"))

        # Work without an explicit transaction commit is discarded when the request ends.
        sessions = get_db_session(resources)
        session = await anext(sessions)
        await session.execute(text("INSERT INTO probe VALUES (1)"))
        await sessions.aclose()

        # Explicit transaction boundary (the pattern use cases follow) persists the change.
        sessions = get_db_session(resources)
        session = await anext(sessions)
        async with session.begin():
            await session.execute(text("INSERT INTO probe VALUES (2)"))
        await sessions.aclose()

        async with resources.engine.connect() as connection:
            rows = (await connection.execute(text("SELECT id FROM probe ORDER BY id"))).scalars().all()
    assert rows == [2]


async def test_shutdown_releases_database_connections(
    migrator: Migrator, database_url: str, redis_url: str
) -> None:
    await migrator.upgrade()
    async with running_app(database_url, redis_url) as (app, client):
        resources: Resources = app.state.resources
        assert (await client.get("/api/v1/health/ready")).status_code == 200
    assert resources.engine.pool.checkedout() == 0
