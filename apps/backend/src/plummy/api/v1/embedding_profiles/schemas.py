"""DTOs for ``/api/v1/embedding-profiles`` (API_CONTRACTS.md §2.8)."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from plummy.application.pagination import Page
from plummy.application.providers.embedding_profiles import ProfileTestReport
from plummy.domain.ai import (
    MAX_DIMENSIONS,
    MAX_MODEL_ID_LENGTH,
    MAX_TOKENS_LIMIT,
    DistanceMetric,
    EmbeddingProfile,
    EmbeddingProfileStatus,
)

MAX_NAME_INPUT_LENGTH = 200


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmbeddingProfileResponse(BaseModel):
    id: UUID
    name: str
    provider_id: UUID
    model_id: str
    dimensions: int
    distance_metric: DistanceMetric
    normalize: bool
    max_input_tokens: int | None
    status: EmbeddingProfileStatus
    created_at: datetime
    updated_at: datetime

    @classmethod
    def build(cls, profile: EmbeddingProfile) -> EmbeddingProfileResponse:
        return cls(
            id=profile.id,
            name=profile.name,
            provider_id=profile.provider_id,
            model_id=profile.model_id,
            dimensions=profile.dimensions,
            distance_metric=profile.distance_metric,
            normalize=profile.normalize,
            max_input_tokens=profile.max_input_tokens,
            status=profile.status,
            created_at=profile.created_at,
            updated_at=profile.updated_at,
        )


class EmbeddingProfileListResponse(BaseModel):
    items: list[EmbeddingProfileResponse]
    next_cursor: str | None
    has_more: bool

    @classmethod
    def from_page(cls, page: Page[EmbeddingProfile]) -> EmbeddingProfileListResponse:
        return cls(
            items=[EmbeddingProfileResponse.build(item) for item in page.items],
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )


class CreateEmbeddingProfileRequest(_Request):
    name: str = Field(min_length=1, max_length=MAX_NAME_INPUT_LENGTH)
    provider_id: UUID
    model_id: str = Field(min_length=1, max_length=MAX_MODEL_ID_LENGTH)
    dimensions: int = Field(ge=1, le=MAX_DIMENSIONS)
    distance_metric: DistanceMetric = DistanceMetric.COSINE
    normalize: bool = False
    max_input_tokens: int | None = Field(default=None, ge=1, le=MAX_TOKENS_LIMIT)


class UpdateEmbeddingProfileRequest(_Request):
    """Only ``name`` and ``status`` change; a different model or vector setting → ``409 reindex_required``."""

    name: str | None = Field(default=None, min_length=1, max_length=MAX_NAME_INPUT_LENGTH)
    status: EmbeddingProfileStatus | None = None
    provider_id: UUID | None = None
    model_id: str | None = Field(default=None, min_length=1, max_length=MAX_MODEL_ID_LENGTH)
    dimensions: int | None = Field(default=None, ge=1, le=MAX_DIMENSIONS)
    distance_metric: DistanceMetric | None = None
    normalize: bool | None = None


class ProfileTestError(BaseModel):
    code: str
    message: str


class EmbeddingProfileTestResponse(BaseModel):
    status: Literal["ok", "failed"]
    latency_ms: int
    tested_at: datetime
    expected_dimensions: int
    actual_dimensions: int | None
    error: ProfileTestError | None

    @classmethod
    def build(cls, report: ProfileTestReport) -> EmbeddingProfileTestResponse:
        error = None
        if report.error_code is not None:
            error = ProfileTestError(code=report.error_code, message=report.error_message or "")
        return cls(
            status=report.status.value,
            latency_ms=report.latency_ms,
            tested_at=report.tested_at,
            expected_dimensions=report.expected_dimensions,
            actual_dimensions=report.actual_dimensions,
            error=error,
        )
