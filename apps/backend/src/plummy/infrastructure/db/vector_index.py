"""``VectorIndexManager`` on PostgreSQL + pgvector (ADR-017 §2, ADR-024).

The services' runtime role has no DDL. Indexes are built and dropped by the schema owner's
``SECURITY DEFINER`` functions of revision ``0017`` (``plummy_ensure_vector_index``,
``plummy_drop_vector_index``): they take only the profile id and build the statement from the stored profile,
with the same expression and predicate as :func:`~plummy.application.knowledge.vector_index.index_spec`. The
build is transactional, not concurrent; an invalid index left by an earlier concurrent build is dropped and
built again.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from plummy.application.knowledge.vector_index import index_name
from plummy.domain.ai import EmbeddingProfile

_STATE = text(
    "SELECT i.indisvalid FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE c.relname = :name AND n.nspname = current_schema()"
)
_ENSURE = text("SELECT public.plummy_ensure_vector_index(:profile_id)")
_DROP = text("SELECT public.plummy_drop_vector_index(:profile_id)")


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
        if await self._valid(index_name(profile.id)) is True:
            return False
        async with self._engine.begin() as connection:
            return bool(await connection.scalar(_ENSURE, {"profile_id": profile.id}))

    async def drop(self, profile_id: UUID) -> bool:
        async with self._engine.begin() as connection:
            return bool(await connection.scalar(_DROP, {"profile_id": profile_id}))
