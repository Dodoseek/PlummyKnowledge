"""Readiness checks for PostgreSQL (implement ``DependencyCheck``)."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


class MigrationsNotAppliedError(Exception):
    pass


class DatabaseCheck:
    name = "database"

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def check(self) -> None:
        async with self._engine.connect() as connection:
            await connection.execute(text("SELECT 1"))


class MigrationsCheck:
    """The database schema is exactly at the head revision(s) shipped with this build."""

    name = "migrations"

    def __init__(self, engine: AsyncEngine, expected_heads: frozenset[str]) -> None:
        self._engine = engine
        self._expected_heads = expected_heads

    async def check(self) -> None:
        async with self._engine.connect() as connection:
            result = await connection.execute(text("SELECT version_num FROM alembic_version"))
            applied = frozenset(result.scalars().all())
        if applied != self._expected_heads:
            raise MigrationsNotAppliedError("database revision does not match expected heads")
