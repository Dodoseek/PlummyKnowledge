"""Hybrid fusion, deduplication, context budget and query normalization (ARCHITECTURE.md §9.4)."""

from __future__ import annotations

from uuid import UUID

import pytest

from plummy.application.errors import InvalidInputError
from plummy.application.retrieval.fusion import deduplicate, fit_budget, reciprocal_rank_fusion
from plummy.application.retrieval.model import ChunkHit, RetrievalSettings
from plummy.application.retrieval.use_cases import normalize_query


def hit(number: int, *, tokens: int = 10, content: str | None = None) -> ChunkHit:
    text = content if content is not None else f"chunk {number}"
    return ChunkHit(
        chunk_id=UUID(int=number),
        document_id=UUID(int=1000 + number),
        version_id=UUID(int=2000 + number),
        data_source_id=UUID(int=3000),
        filename=f"doc{number}.md",
        chunk_index=0,
        content=text,
        content_hash=text.encode().ljust(32, b"\0")[:32],
        token_count=tokens,
        page_start=None,
        page_end=None,
        section_path=(),
        sheet_name=None,
        row_start=None,
        row_end=None,
        score=0.0,
    )


def ids(ranked: object) -> list[int]:
    return [item.hit.chunk_id.int for item in ranked]  # type: ignore[attr-defined]


# --- reciprocal rank fusion -----------------------------------------------------------------------------


def test_chunk_found_by_both_branches_sums_both_contributions() -> None:
    ranked = reciprocal_rank_fusion([hit(1), hit(2)], [hit(2), hit(3)], k=60)
    assert ids(ranked) == [2, 1, 3]
    both = ranked[0]
    assert (both.vector_rank, both.keyword_rank) == (2, 1)
    assert both.fused_score == pytest.approx(1 / 62 + 1 / 61)
    assert ranked[1].keyword_rank is None
    assert ranked[2].vector_rank is None


def test_ties_are_broken_deterministically() -> None:
    # Rank 1 in one branch each: equal scores, equal best rank -> chunk id order, every time.
    first = reciprocal_rank_fusion([hit(7)], [hit(3)], k=60)
    second = reciprocal_rank_fusion([hit(7)], [hit(3)], k=60)
    assert ids(first) == ids(second) == [3, 7]


def test_smaller_k_rewards_top_ranks_more() -> None:
    vector, keyword = [hit(1), hit(2), hit(3)], [hit(3), hit(4)]
    assert ids(reciprocal_rank_fusion(vector, keyword, k=1))[0] == 3
    assert reciprocal_rank_fusion(vector, keyword, k=1)[0].fused_score == pytest.approx(1 / 4 + 1 / 2)


def test_empty_branches() -> None:
    assert reciprocal_rank_fusion([], [], k=60) == []
    assert ids(reciprocal_rank_fusion([], [hit(5)], k=60)) == [5]


def test_a_repeated_hit_in_one_branch_counts_once() -> None:
    ranked = reciprocal_rank_fusion([hit(1), hit(1)], [], k=60)
    assert ids(ranked) == [1]
    assert ranked[0].fused_score == pytest.approx(1 / 61)


# --- deduplication and budget -----------------------------------------------------------------------------


def test_same_text_in_two_documents_is_kept_once_at_its_best_rank() -> None:
    ranked = reciprocal_rank_fusion([hit(1, content="same"), hit(2), hit(3, content="same")], [], k=60)
    assert ids(deduplicate(ranked)) == [1, 2]


def test_budget_skips_a_chunk_that_does_not_fit_and_takes_a_smaller_one() -> None:
    ranked = reciprocal_rank_fusion([hit(1, tokens=60), hit(2, tokens=50), hit(3, tokens=30)], [], k=60)
    selection = fit_budget(ranked, top_k=8, max_tokens=100)
    assert ids(selection.chunks) == [1, 3]
    assert (selection.total_tokens, selection.skipped_over_budget) == (90, 1)


def test_budget_exactly_full_and_top_k_limit() -> None:
    ranked = reciprocal_rank_fusion([hit(1, tokens=50), hit(2, tokens=50), hit(3, tokens=1)], [], k=60)
    assert ids(fit_budget(ranked, top_k=8, max_tokens=100).chunks) == [1, 2]  # the third would exceed
    assert ids(fit_budget(ranked, top_k=1, max_tokens=1000).chunks) == [1]


def test_a_chunk_larger_than_the_whole_budget_is_never_taken() -> None:
    ranked = reciprocal_rank_fusion([hit(1, tokens=5000)], [], k=60)
    selection = fit_budget(ranked, top_k=8, max_tokens=4000)
    assert selection.chunks == ()
    assert selection.skipped_over_budget == 1


# --- settings and query -----------------------------------------------------------------------------------


def test_default_settings_match_the_contract() -> None:
    settings = RetrievalSettings()
    assert (settings.top_k, settings.vector_top_k, settings.keyword_top_k, settings.rrf_k) == (8, 40, 40, 60)
    assert settings.max_context_tokens == 8000


@pytest.mark.parametrize(
    "overrides",
    [
        {"top_k": 0},
        {"top_k": 51},
        {"top_k": 10, "vector_top_k": 9},
        {"keyword_top_k": 201},
        {"rrf_k": 0},
        {"max_context_tokens": 255},
    ],
)
def test_settings_out_of_range_are_rejected(overrides: dict[str, int]) -> None:
    with pytest.raises(ValueError, match="must be"):
        RetrievalSettings(**overrides)


def test_query_is_normalized() -> None:
    assert normalize_query("  отпуск\t сотрудника \n\x00 2024 ") == "отпуск сотрудника 2024"
    assert normalize_query("\u0435\u0308") == "\u0451"  # NFC: Cyrillic ie + combining diaeresis is yo


@pytest.mark.parametrize(("query", "code"), [("   \n\t", "empty_query"), ("x" * 1001, "query_too_long")])
def test_invalid_queries_are_rejected(query: str, code: str) -> None:
    with pytest.raises(InvalidInputError) as raised:
        normalize_query(query)
    assert raised.value.error_code == code


def test_query_of_exactly_the_limit_is_accepted() -> None:
    assert len(normalize_query("x" * 1000)) == 1000
