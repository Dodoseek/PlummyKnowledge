"""Per-profile HNSW index (ADR-017 §2): naming, DDL and the ``ensure_vector_index`` use case.

The DDL is assembled only from a stored, domain-validated profile: a UUID, an integer 1-4000 and a metric from
the enum. No value from a request reaches it.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from plummy.application.ports import UnitOfWorkFactory, VectorIndexManager
from plummy.domain.ai import MAX_DIMENSIONS, DistanceMetric, EmbeddingProfile, EmbeddingProfileStatus

logger = logging.getLogger(__name__)

INDEX_PREFIX: Final = "ix_embeddings_hnsw_"
FULL_PRECISION_LIMIT: Final = 2000  # pgvector indexes `vector` up to 2000 dimensions, `halfvec` up to 4000
_OPS: Final = {
    DistanceMetric.COSINE: "cosine_ops",
    DistanceMetric.INNER_PRODUCT: "ip_ops",
    DistanceMetric.L2: "l2_ops",
}


def index_name(profile_id: UUID) -> str:
    return INDEX_PREFIX + profile_id.hex


@dataclass(frozen=True, slots=True)
class VectorIndexSpec:
    name: str
    expression: str  # also used verbatim by the retrieval query (ADR-017 §3)
    operator_class: str
    predicate: str

    def create_sql(self) -> str:
        return (
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {self.name} ON embeddings "
            f"USING hnsw (({self.expression}) {self.operator_class}) WHERE {self.predicate}"
        )


def index_spec(profile: EmbeddingProfile) -> VectorIndexSpec:
    dimensions, profile_id = profile.dimensions, profile.id
    if not isinstance(dimensions, int) or not 1 <= dimensions <= MAX_DIMENSIONS:
        raise ValueError("profile dimensions are not indexable")
    if not isinstance(profile_id, UUID):
        raise TypeError("profile id must be a UUID")
    kind = "vector" if dimensions <= FULL_PRECISION_LIMIT else "halfvec"
    return VectorIndexSpec(
        name=index_name(profile_id),
        expression=f"embedding::{kind}({dimensions})",
        operator_class=f"{kind}_{_OPS[DistanceMetric(profile.distance_metric)]}",
        predicate=f"embedding_config_id = '{profile_id}'",
    )


class EnsureVectorIndex:
    """Idempotent; run from the ``maintenance`` queue when a knowledge base starts using a profile."""

    def __init__(self, uow: UnitOfWorkFactory, indexes: VectorIndexManager) -> None:
        self._uow, self._indexes = uow, indexes

    async def execute(self, profile_id: UUID) -> bool:
        async with self._uow() as uow:
            profile = await uow.embedding_profiles.get(profile_id)
        if profile is None:
            return False
        if profile.status is EmbeddingProfileStatus.RETIRED:
            # Vectors of a retired profile are removed by reindexing; its index goes with them (ADR-017 §2).
            return False
        created = await self._indexes.ensure(profile)
        if created:
            logger.info("vector index created", extra={"embedding_profile_id": str(profile_id)})
        return created
