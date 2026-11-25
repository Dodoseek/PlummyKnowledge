"""Retrieval values (ARCHITECTURE.md §9.4, DATABASE_SCHEMA.md §5.11).

``RetrievalScope`` is built only by the access policy (``use_cases.resolve_scope``) from the verified session:
no identifier from a client reaches the SQL filters directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Final
from uuid import UUID

from plummy.domain.ai import EmbeddingProfile

MAX_QUERY_LENGTH: Final = 1000
MAX_KNOWLEDGE_BASES: Final = 20


class SearchMode(StrEnum):
    HYBRID = "hybrid"
    VECTOR = "vector"  # evaluation ablations only
    KEYWORD = "keyword"


@dataclass(frozen=True, slots=True)
class RetrievalSettings:
    """Defaults from API_CONTRACTS.md §4 (assistant ``retrieval``); later part of the assistant profile."""

    top_k: int = 8
    vector_top_k: int = 40
    keyword_top_k: int = 40
    rrf_k: int = 60
    max_context_tokens: int = 8000

    def __post_init__(self) -> None:
        if not 1 <= self.top_k <= 50:
            raise ValueError("top_k must be 1-50")
        if not self.top_k <= self.vector_top_k <= 200 or not self.top_k <= self.keyword_top_k <= 200:
            raise ValueError("branch limits must be top_k-200")
        if not 1 <= self.rrf_k <= 1000:
            raise ValueError("rrf_k must be 1-1000")
        if not 256 <= self.max_context_tokens <= 128_000:
            raise ValueError("max_context_tokens must be 256-128000")


@dataclass(frozen=True, slots=True)
class RetrievalScope:
    """What a search may read: the sources of authorized knowledge bases and their one embedding profile."""

    knowledge_base_ids: frozenset[UUID]
    source_ids: frozenset[UUID]
    profile: EmbeddingProfile


@dataclass(frozen=True, slots=True)
class ChunkHit:
    """One chunk found by a branch; ``score`` orders hits within that branch only."""

    chunk_id: UUID
    document_id: UUID
    version_id: UUID
    data_source_id: UUID
    filename: str
    chunk_index: int
    content: str
    content_hash: bytes
    token_count: int
    page_start: int | None
    page_end: int | None
    section_path: tuple[str, ...]
    sheet_name: str | None
    row_start: int | None
    row_end: int | None
    score: float


@dataclass(frozen=True, slots=True)
class RankedChunk:
    hit: ChunkHit
    fused_score: float
    vector_rank: int | None = None  # 1-based rank in the vector branch
    keyword_rank: int | None = None


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    chunks: tuple[RankedChunk, ...]
    mode: SearchMode
    total_tokens: int
    vector_candidates: int = 0
    keyword_candidates: int = 0
    keyword_query_empty: bool = False
    skipped_over_budget: int = field(default=0)
