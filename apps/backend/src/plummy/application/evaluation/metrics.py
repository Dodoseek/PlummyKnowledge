"""Retrieval metrics: Recall@K, MRR, nDCG@K (ARCHITECTURE.md §15, docs/evaluation/). Pure functions.

A question names its relevant facts as ``(document, evidence)`` pairs. A retrieved chunk *covers* a fact when
it comes from that document and its text contains the evidence (case, whitespace and the Cyrillic yo/ie
spelling ignored), so the judgement does not depend on chunk boundaries or chunk ids, which change with every
reindex.

- ``recall@k`` — share of the facts covered by the first ``k`` chunks.
- ``reciprocal rank`` — ``1 / rank`` of the first chunk that covers any fact (0 if none does).
- ``ndcg@k`` — the gain of a chunk is the number of facts it covers *first*; the ideal list covers one new
  fact per position. Clamped to 1, which a chunk covering several facts at once can exceed.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass


def normalize_text(text: str) -> str:
    return " ".join(text.casefold().replace("\u0451", "\u0435").split())  # yo -> ie, as in ADR-018


@dataclass(frozen=True, slots=True)
class Fact:
    document: str
    evidence: str


@dataclass(frozen=True, slots=True)
class RetrievedChunk:
    filename: str
    content: str


def new_facts_per_rank(chunks: Sequence[RetrievedChunk], facts: Sequence[Fact]) -> list[int]:
    """For each position, how many facts that chunk covers for the first time."""
    wanted = [(fact.document, normalize_text(fact.evidence)) for fact in facts]
    covered: set[int] = set()
    gains = []
    for chunk in chunks:
        text = normalize_text(chunk.content)
        new = {
            index
            for index, (document, evidence) in enumerate(wanted)
            if index not in covered and chunk.filename == document and evidence in text
        }
        covered |= new
        gains.append(len(new))
    return gains


def recall_at(k: int, gains: Sequence[int], relevant: int) -> float:
    if relevant == 0:
        raise ValueError("recall is undefined without relevant facts")
    return min(1.0, sum(gains[:k]) / relevant)


def reciprocal_rank(gains: Sequence[int]) -> float:
    for rank, gain in enumerate(gains, start=1):
        if gain:
            return 1.0 / rank
    return 0.0


def ndcg_at(k: int, gains: Sequence[int], relevant: int) -> float:
    if relevant == 0:
        raise ValueError("nDCG is undefined without relevant facts")
    dcg = sum(gain / math.log2(rank + 1) for rank, gain in enumerate(gains[:k], start=1))
    ideal = sum(1 / math.log2(rank + 1) for rank in range(1, min(k, relevant) + 1))
    return min(1.0, dcg / ideal)


def mean(values: Sequence[float]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None
