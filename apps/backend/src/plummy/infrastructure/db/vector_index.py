"""``VectorIndexManager`` on PostgreSQL + pgvector (ADR-017 §2).

``CREATE INDEX CONCURRENTLY`` cannot run inside a transaction, so every statement here uses its own
connection in autocommit mode. An interrupted concurrent build leaves an invalid index behind
(``pg_index.indisvalid = false``); it is dropped and built again.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from plummy.application.knowledge.vector_index import index_name, index_spec
from plummy.domain.ai import EmbeddingProfile

_STATE = text(
    "SELECT i.indisvalid FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE c.relname = :name AND n.nspname = current_schema()"
)


class PgVectorIndexManager:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    async def _valid(self, name: str) -> bool | None:
        async with self._engine.connect() as connection:
            value = await connection.scalar(_STATE, {"name": name})
        return None if value is None else bool(value)

    async def is_ready(self, profile: EmbeddingProfile) -> bool:
        return bool(await self._valid(index_name(profile.id)))

    async def ensure(self, profile: EmbeddingProfile) -> bool:
        spec = index_spec(profile)
        state = await self._valid(spec.name)
        if state is True:
            return False
        async with self._engine.connect() as connection:
            autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
            if state is False:
                await autocommit.execute(text(f"DROP INDEX CONCURRENTLY IF EXISTS {spec.name}"))
            await autocommit.execute(text(spec.create_sql()))
        return True

    async def drop(self, profile_id: UUID) -> bool:
        name = index_name(profile_id)
        if await self._valid(name) is None:
            return False
        async with self._engine.connect() as connection:
            autocommit = await connection.execution_options(isolation_level="AUTOCOMMIT")
            await autocommit.execute(text(f"DROP INDEX CONCURRENTLY IF EXISTS {name}"))
        return True
