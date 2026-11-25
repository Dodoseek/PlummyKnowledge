"""Hybrid fusion, deduplication and the context budget (ARCHITECTURE.md §9.4 п.3-4). Pure functions.

Reciprocal Rank Fusion combines ranks, not scores: cosine distances and ``ts_rank_cd`` values are not
comparable, ranks are. A chunk found by both branches sums both contributions.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from plummy.application.retrieval.model import ChunkHit, RankedChunk


def reciprocal_rank_fusion(
    vector_hits: Sequence[ChunkHit], keyword_hits: Sequence[ChunkHit], *, k: int
) -> list[RankedChunk]:
    """``score = Σ 1 / (k + rank)`` over the branches that found the chunk; ties keep a stable order
    (better best rank first, then chunk id) so the same input always gives the same ranking."""
    hits: dict[UUID, ChunkHit] = {}
    vector_ranks: dict[UUID, int] = {}
    keyword_ranks: dict[UUID, int] = {}
    for rank, hit in enumerate(vector_hits, start=1):
        if hit.chunk_id not in vector_ranks:
            vector_ranks[hit.chunk_id] = rank
            hits.setdefault(hit.chunk_id, hit)
    for rank, hit in enumerate(keyword_hits, start=1):
        if hit.chunk_id not in keyword_ranks:
            keyword_ranks[hit.chunk_id] = rank
            hits.setdefault(hit.chunk_id, hit)

    ranked = []
    for chunk_id, hit in hits.items():
        vector_rank, keyword_rank = vector_ranks.get(chunk_id), keyword_ranks.get(chunk_id)
        score = sum(1.0 / (k + rank) for rank in (vector_rank, keyword_rank) if rank is not None)
        ranked.append(RankedChunk(hit, score, vector_rank, keyword_rank))
    ranked.sort(key=_order)
    return ranked


def _order(item: RankedChunk) -> tuple[float, int, str]:
    best = min(rank for rank in (item.vector_rank, item.keyword_rank) if rank is not None)
    return (-item.fused_score, best, str(item.hit.chunk_id))


def deduplicate(ranked: Sequence[RankedChunk]) -> list[RankedChunk]:
    """Keeps the best-ranked chunk of each ``content_hash``: the same text in two documents (or two
    versions copied between sources) would waste the context budget and split citations."""
    seen: set[bytes] = set()
    unique = []
    for item in ranked:
        if item.hit.content_hash in seen:
            continue
        seen.add(item.hit.content_hash)
        unique.append(item)
    return unique


@dataclass(frozen=True, slots=True)
class Selection:
    chunks: tuple[RankedChunk, ...]
    total_tokens: int
    skipped_over_budget: int


def fit_budget(ranked: Sequence[RankedChunk], *, top_k: int, max_tokens: int) -> Selection:
    """Takes chunks in rank order while they fit: a chunk larger than the remaining budget is skipped and a
    smaller, lower-ranked one may still fit. Never more than ``top_k`` chunks or ``max_tokens`` tokens."""
    chosen: list[RankedChunk] = []
    used = skipped = 0
    for item in ranked:
        if len(chosen) == top_k:
            break
        tokens = max(0, item.hit.token_count)
        if used + tokens > max_tokens:
            skipped += 1
            continue
        chosen.append(item)
        used += tokens
    return Selection(tuple(chosen), used, skipped)
