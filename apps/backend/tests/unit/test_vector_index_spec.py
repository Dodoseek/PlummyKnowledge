"""ADR-017 §2: index name, expression type and operator class come only from the validated profile."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest

from plummy.application.knowledge.vector_index import index_name, index_spec
from plummy.domain.ai import DistanceMetric, EmbeddingProfile, EmbeddingProfileStatus

PROFILE_ID = UUID("01a1f2cf-f3fa-7871-b146-79ace4bd7e8a")
NOW = datetime(2026, 11, 19, tzinfo=UTC)


def profile(dimensions: int = 1536, metric: DistanceMetric = DistanceMetric.COSINE) -> EmbeddingProfile:
    return EmbeddingProfile(
        id=PROFILE_ID,
        name="E",
        provider_id=PROFILE_ID,
        model_id="m",
        dimensions=dimensions,
        distance_metric=metric,
        normalize=True,
        max_input_tokens=None,
        status=EmbeddingProfileStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
    )


def test_name_is_deterministic_and_short() -> None:
    name = index_name(PROFILE_ID)
    assert name == "ix_embeddings_hnsw_01a1f2cff3fa7871b14679ace4bd7e8a"
    assert len(name) < 63


@pytest.mark.parametrize(
    ("dimensions", "metric", "expression", "ops"),
    [
        (1536, DistanceMetric.COSINE, "embedding::vector(1536)", "vector_cosine_ops"),
        (2000, DistanceMetric.L2, "embedding::vector(2000)", "vector_l2_ops"),
        (2001, DistanceMetric.INNER_PRODUCT, "embedding::halfvec(2001)", "halfvec_ip_ops"),
        (4000, DistanceMetric.COSINE, "embedding::halfvec(4000)", "halfvec_cosine_ops"),
        (1, DistanceMetric.COSINE, "embedding::vector(1)", "vector_cosine_ops"),
    ],
)
def test_expression_by_dimensions_and_metric(
    dimensions: int, metric: DistanceMetric, expression: str, ops: str
) -> None:
    spec = index_spec(profile(dimensions, metric))
    assert (spec.expression, spec.operator_class) == (expression, ops)
    assert spec.predicate == f"embedding_config_id = '{PROFILE_ID}'"
    assert spec.create_sql() == (
        f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {spec.name} ON embeddings "
        f"USING hnsw (({expression}) {ops}) WHERE embedding_config_id = '{PROFILE_ID}'"
    )


@pytest.mark.parametrize("dimensions", [0, 4001, 16000])
def test_unindexable_dimensions_are_refused(dimensions: int) -> None:
    with pytest.raises(ValueError, match="indexable"):
        index_spec(profile(dimensions))


def test_only_typed_values_reach_the_ddl() -> None:
    hostile = replace(profile(), id="x'; DROP TABLE embeddings; --")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        index_spec(hostile)
    with pytest.raises((ValueError, TypeError)):
        index_spec(replace(profile(), dimensions="1536); DROP TABLE x; --"))  # type: ignore[arg-type]
