"""Retrieval metrics, dataset validation and the ``eval run`` arguments (docs/evaluation/)."""

from __future__ import annotations

import io
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from plummy.application.evaluation.dataset import DatasetError, EvalQuestion, parse_dataset
from plummy.application.evaluation.metrics import (
    Fact,
    RetrievedChunk,
    mean,
    ndcg_at,
    new_facts_per_rank,
    recall_at,
    reciprocal_rank,
)
from plummy.application.evaluation.runner import score_question, summarize
from plummy.cli.app import main

DATASET = Path(__file__).resolve().parents[2] / "evaluation" / "datasets" / "synthetic-v1"


def chunk(filename: str, content: str) -> RetrievedChunk:
    return RetrievedChunk(filename, content)


# --- metrics ------------------------------------------------------------------------------------------------


def test_a_fact_is_covered_by_its_document_regardless_of_case_and_whitespace() -> None:
    facts = [Fact("a.md", "Пять  ДНЕЙ " + "\n" + " отпуска"), Fact("b.md", "28 days")]
    chunks = [
        chunk("b.md", "пять дней отпуска"),  # right text, wrong document
        chunk("a.md", "до пяти дней отпуска, пять дней отпуска"),
        chunk("b.md", "We give 28   DAYS."),
        chunk("b.md", "28 days again"),  # already covered: no new gain
    ]
    assert new_facts_per_rank(chunks, facts) == [0, 1, 1, 0]


def test_yo_and_ie_are_the_same_letter() -> None:
    assert new_facts_per_rank([chunk("a.md", "ещё")], [Fact("a.md", "еще")]) == [1]
    assert new_facts_per_rank([chunk("a.md", "еще")], [Fact("a.md", "ЕЩЁ")]) == [1]


def test_one_chunk_may_cover_several_facts() -> None:
    facts = [Fact("t.csv", "alpha"), Fact("t.csv", "beta"), Fact("t.csv", "gamma")]
    gains = new_facts_per_rank([chunk("t.csv", "alpha; beta; gamma")], facts)
    assert gains == [3]
    assert recall_at(1, gains, 3) == 1.0
    assert ndcg_at(1, gains, 3) == 1.0  # clamped: one chunk did the work of three


def test_recall_mrr_and_ndcg_values() -> None:
    gains = [0, 1, 0, 1]
    assert recall_at(1, gains, 2) == 0.0
    assert recall_at(3, gains, 2) == 0.5
    assert recall_at(10, gains, 2) == 1.0
    assert reciprocal_rank(gains) == 0.5
    ideal = 1 + 1 / math.log2(3)
    assert ndcg_at(4, gains, 2) == pytest.approx((1 / math.log2(3) + 1 / math.log2(5)) / ideal)
    assert ndcg_at(1, [1], 1) == 1.0
    assert ndcg_at(2, [0, 1], 1) == pytest.approx(1 / math.log2(3))


def test_nothing_found() -> None:
    assert reciprocal_rank([0, 0]) == 0.0
    assert reciprocal_rank([]) == 0.0
    assert recall_at(5, [], 1) == 0.0
    assert ndcg_at(5, [], 1) == 0.0


def test_metrics_without_relevant_facts_are_undefined() -> None:
    with pytest.raises(ValueError, match="undefined"):
        recall_at(1, [0], 0)
    with pytest.raises(ValueError, match="undefined"):
        ndcg_at(1, [0], 0)
    assert mean([]) is None
    assert mean([0.5, 1.0]) == 0.75


def test_scoring_and_summary_skip_unanswerable_questions() -> None:
    answerable = EvalQuestion("q1", "?", "en", ("similar",), (Fact("a.md", "x"),))
    unanswerable = EvalQuestion("q2", "?", "en", ("unanswerable",), ())
    first = score_question(answerable, [chunk("b.md", "x"), chunk("a.md", "x")], (1, 3))
    second = score_question(unanswerable, [chunk("a.md", "x")], (1, 3))
    assert (first.recall, first.reciprocal_rank, first.first_relevant_rank) == ({1: 0.0, 3: 1.0}, 0.5, 2)
    assert second.reciprocal_rank is None
    assert second.ranked_files == ["a.md"]
    summary = summarize([first, second], (1, 3), ["all", "similar", "unanswerable"])
    assert summary["all"] == {
        "questions": 1,
        "recall": {"1": 0.0, "3": 1.0},
        "mrr": 0.5,
        "ndcg": summary["all"]["ndcg"],
    }
    assert "unanswerable" not in summary
    assert set(first.to_dict()) >= {"id", "recall", "reciprocal_rank", "ndcg", "ranked_files"}


# --- dataset ------------------------------------------------------------------------------------------------


def manifest(**overrides: Any) -> bytes:
    value: dict[str, Any] = {
        "name": "tiny",
        "documents": ["a.md"],
        "questions": [
            {
                "id": "q1",
                "question": "Q?",
                "language": "ru",
                "relevant": [{"document": "a.md", "evidence": "x y"}],
            }
        ],
    }
    value.update(overrides)
    return json.dumps(value).encode()


FILES = {"a.md": b"# A\n\nx\ny\n"}


def test_a_valid_manifest() -> None:
    dataset = parse_dataset(manifest(), FILES)
    assert dataset.name == "tiny"
    assert [d.filename for d in dataset.documents] == ["a.md"]
    assert dataset.questions[0].facts == (Fact("a.md", "x y"),)
    assert len(dataset.fingerprint) == 64


def test_the_fingerprint_follows_every_byte() -> None:
    first = parse_dataset(manifest(), FILES).fingerprint
    assert parse_dataset(manifest(), {"a.md": b"# A\n\nx\ny\n\n"}).fingerprint != first
    assert parse_dataset(manifest(description="d"), FILES).fingerprint != first
    assert parse_dataset(manifest(), FILES).fingerprint == first


@pytest.mark.parametrize(
    ("raw", "files", "message"),
    [
        (b"{", FILES, "not valid JSON"),
        (b"[]", FILES, "must be an object"),
        (manifest(documents=[]), FILES, "non-empty list"),
        (manifest(documents=["a.md", "a.md"]), FILES, "duplicate file name"),
        (manifest(documents=["b.md"]), FILES, "b.md is missing"),
        (manifest(), {"a.md": b"\xff\xfe"}, "not UTF-8"),
        (manifest(questions=[]), FILES, "questions"),
        (
            manifest(questions=[{"id": "q1", "question": "Q", "language": "de"}]),
            FILES,
            "language",
        ),
        (
            manifest(
                questions=[
                    {"id": "q1", "question": "Q", "language": "en"},
                    {"id": "q1", "question": "Q", "language": "en"},
                ]
            ),
            FILES,
            "duplicate id",
        ),
        (
            manifest(
                questions=[
                    {
                        "id": "q1",
                        "question": "Q",
                        "language": "en",
                        "relevant": [{"document": "a.md", "evidence": "not there"}],
                    }
                ]
            ),
            FILES,
            "does not occur",
        ),
        (
            manifest(
                questions=[
                    {
                        "id": "q1",
                        "question": "Q",
                        "language": "en",
                        "relevant": [{"document": "c.md", "evidence": "x"}],
                    }
                ]
            ),
            FILES,
            "not in the dataset",
        ),
        (manifest(questions=[{"id": "q1", "question": " ", "language": "en"}]), FILES, "non-empty string"),
    ],
)
def test_invalid_manifests_are_rejected(raw: bytes, files: dict[str, bytes], message: str) -> None:
    with pytest.raises(DatasetError, match=message):
        parse_dataset(raw, files)


def test_the_shipped_dataset_is_valid_and_covers_the_planned_cases() -> None:
    files = {path.name: path.read_bytes() for path in (DATASET / "documents").iterdir()}
    dataset = parse_dataset((DATASET / "manifest.json").read_bytes(), files)
    assert 30 <= len(dataset.documents) <= 50
    assert 50 <= len(dataset.questions) <= 100
    assert sorted(files) == sorted(d.filename for d in dataset.documents)  # no stray files
    formats = Counter(Path(d.filename).suffix for d in dataset.documents)
    assert set(formats) == {".md", ".txt", ".csv"}
    assert {q.language for q in dataset.questions} == {"ru", "en"}
    tags = Counter(tag for q in dataset.questions for tag in q.tags)
    assert tags["unanswerable"] >= 5
    for tag in ("similar", "conflict", "cross_language", "table", "multi"):
        assert tags[tag] >= 2, tag
    assert all(q.answerable is ("unanswerable" not in q.tags) for q in dataset.questions)


# --- command line -------------------------------------------------------------------------------------------


def test_eval_run_rejects_an_invalid_dataset_before_connecting(tmp_path: Path) -> None:
    (tmp_path / "documents").mkdir()
    (tmp_path / "manifest.json").write_bytes(manifest())
    err = io.StringIO()

    def no_services(_: object) -> Any:
        raise AssertionError("must not connect")

    code = main(
        ["eval", "run", "--dataset", str(tmp_path), "--email", "a@example.com",
         "--embedding-profile", "00000000-0000-0000-0000-000000000001"],
        evaluation_factory=no_services,
        settings=object(),  # type: ignore[arg-type]
        stdout=io.StringIO(),
        stderr=err,
    )  # fmt: skip
    assert code == 1
    assert "dataset: documents: a.md is missing" in err.getvalue()


@pytest.mark.parametrize("value", ["0", "51", "a,b", ""])
def test_eval_run_rejects_bad_cut_offs(value: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(
            ["eval", "run", "--dataset", ".", "--email", "a@example.com",
             "--embedding-profile", "00000000-0000-0000-0000-000000000001", "--k", value]
        )  # fmt: skip
    err = capsys.readouterr().err
    assert "cut-offs" in err or "integers" in err
