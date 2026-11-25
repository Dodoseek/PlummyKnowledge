"""``ChunkSearch`` on PostgreSQL: pgvector (ADR-017 §3) and full-text search (ADR-018).

Both branches repeat every filter of DATABASE_SCHEMA.md §5.11 inside the SQL statement: only the scope's
sources, live documents, their current ``ready`` version and its active ``chunk_set``. Nothing is filtered
after the query, so a chunk outside the scope can never be returned, ranked or counted.

The vector branch uses the expression and the predicate of the profile's partial HNSW index verbatim (the
profile UUID and dimensions are literals from the stored, validated profile; the query vector and the
source ids are parameters). With few candidates an exact scan is cheaper and exact, so the index is turned
off for that statement; otherwise an iterative index scan keeps filtering from starving the result, and the
candidates are sorted again by exact distance.

The keyword branch matches **any** term of the query (ADR-018, amendment of the evaluation baseline): the
query is parsed with ``plainto_tsquery`` (no operator syntax: quotes, ``-``, ``or`` are plain words) and its
``&`` are turned into ``|``; ``ts_rank`` (normalized by the log of the chunk length) then ranks chunks with
more of the query's terms first; ``ts_rank_cd`` ties such chunks under OR. A natural question rarely has all
its words in one chunk, so AND semantics found almost nothing.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Final

from sqlalchemy import bindparam, text
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession

from plummy.application.knowledge.vector_index import index_spec
from plummy.application.retrieval.model import ChunkHit, RetrievalScope
from plummy.domain.ai import DistanceMetric
from plummy.infrastructure.db.models import FTS_CONFIG
from plummy.infrastructure.db.types import vector_literal

EXACT_SCAN_THRESHOLD: Final = 10_000  # ADR-017 §3: below this many candidate vectors, no index
MAX_SCAN_TUPLES: Final = 20_000
MAX_EF_SEARCH: Final = 1000

# The access and version filters of DATABASE_SCHEMA.md §5.11, shared by both branches.
_SCOPE_FILTER: Final = """
      c.data_source_id = ANY(:sources)
  AND d.data_source_id = ANY(:sources)
  AND d.deleted_at IS NULL
  AND d.current_version_id = c.document_version_id
  AND v.status = 'ready'
  AND c.chunk_set = v.active_chunk_set
"""

_COLUMNS: Final = """
  c.id, c.document_id, c.document_version_id, d.data_source_id, d.filename, c.chunk_index, c.content,
  c.content_hash, c.token_count, c.page_start, c.page_end, c.section_path, c.sheet_name, c.row_start,
  c.row_end
"""

_SOURCES = bindparam("sources", type_=ARRAY(PG_UUID(as_uuid=True)))

# Rendered tsquery text quotes every lexeme, so swapping the operators re-parses safely.
_ANY_TERM: Final = f"replace(plainto_tsquery('{FTS_CONFIG}', :query)::text, ' & ', ' | ')::tsquery"

_KEYWORDS = text(
    f"""
    WITH q AS (SELECT {_ANY_TERM} AS query)
    SELECT {_COLUMNS}, ts_rank(c.search_vector, q.query, 1) AS score
    FROM q, document_chunks c
    JOIN documents d ON d.id = c.document_id
    JOIN document_versions v ON v.id = c.document_version_id
    WHERE c.search_vector @@ q.query AND {_SCOPE_FILTER}
    ORDER BY score DESC, c.id
    LIMIT :limit
    """  # noqa: S608 - a constant: the expression and the filter are module literals, values are parameters
).bindparams(_SOURCES)

_LEXEMES = text(f"SELECT numnode({_ANY_TERM})")

_CANDIDATES = text(
    "SELECT count(*) FROM embeddings e "
    "WHERE e.embedding_config_id = :profile AND e.data_source_id = ANY(:sources)"
).bindparams(_SOURCES)


def _similarity(metric: DistanceMetric, distance: float) -> float:
    """Higher is closer, for display and thresholds: cosine similarity, inner product, negative L2."""
    if metric is DistanceMetric.COSINE:
        return 1.0 - distance
    return -distance


def _hit(row: Any, score: float) -> ChunkHit:
    return ChunkHit(
        chunk_id=row.id,
        document_id=row.document_id,
        version_id=row.document_version_id,
        data_source_id=row.data_source_id,
        filename=row.filename,
        chunk_index=row.chunk_index,
        content=row.content,
        content_hash=bytes(row.content_hash),
        token_count=row.token_count,
        page_start=row.page_start,
        page_end=row.page_end,
        section_path=tuple(row.section_path or ()),
        sheet_name=row.sheet_name,
        row_start=row.row_start,
        row_end=row.row_end,
        score=score,
    )


class SqlChunkSearch:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session

    async def by_vector(
        self, scope: RetrievalScope, vector: Sequence[float], *, limit: int
    ) -> list[ChunkHit]:
        if not scope.source_ids or limit < 1:
            return []
        profile = scope.profile
        spec = index_spec(profile)  # validated UUID, dimensions and metric only
        distance = f"(e.{spec.expression} {spec.distance_operator} CAST(:query AS {spec.vector_type}))"
        sources = sorted(scope.source_ids)
        candidates = await self._db.scalar(_CANDIDATES, {"profile": profile.id, "sources": sources})
        if (candidates or 0) < EXACT_SCAN_THRESHOLD:
            await self._db.execute(text("SET LOCAL enable_indexscan = off"))
        else:
            await self._db.execute(text("SET LOCAL hnsw.iterative_scan = relaxed_order"))
            await self._db.execute(text(f"SET LOCAL hnsw.max_scan_tuples = {MAX_SCAN_TUPLES}"))
            await self._db.execute(text(f"SET LOCAL hnsw.ef_search = {min(MAX_EF_SEARCH, max(40, limit))}"))
        statement = text(
            f"""
            WITH candidates AS MATERIALIZED (
              SELECT e.chunk_id, {distance} AS distance
              FROM embeddings e
              JOIN document_chunks c ON c.id = e.chunk_id
              JOIN documents d ON d.id = c.document_id
              JOIN document_versions v ON v.id = c.document_version_id
              WHERE e.{spec.predicate} AND e.data_source_id = ANY(:sources) AND {_SCOPE_FILTER}
              ORDER BY {distance}
              LIMIT :limit
            )
            SELECT {_COLUMNS}, cand.distance
            FROM candidates cand
            JOIN document_chunks c ON c.id = cand.chunk_id
            JOIN documents d ON d.id = c.document_id
            ORDER BY cand.distance, c.id
            """  # noqa: S608 - expression, operator, cast and predicate come from index_spec only
        ).bindparams(_SOURCES)
        rows = await self._db.execute(
            statement, {"query": vector_literal(vector), "sources": sources, "limit": limit}
        )
        metric = DistanceMetric(profile.distance_metric)
        return [_hit(row, _similarity(metric, float(row.distance))) for row in rows]

    async def by_keywords(self, scope: RetrievalScope, query: str, *, limit: int) -> list[ChunkHit] | None:
        if not await self._db.scalar(_LEXEMES, {"query": query}):
            return None  # only stop words or punctuation: nothing to match (ADR-018 §2)
        if not scope.source_ids or limit < 1:
            return []
        rows = await self._db.execute(
            _KEYWORDS, {"query": query, "sources": sorted(scope.source_ids), "limit": limit}
        )
        return [_hit(row, float(row.score)) for row in rows]
