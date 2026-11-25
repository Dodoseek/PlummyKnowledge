"""An evaluation dataset: documents and questions with the facts that answer them (docs/evaluation/).

The manifest is checked strictly before anything is uploaded: every referenced document exists, every
evidence string really occurs in its document, ids are unique. A mistake in the dataset would otherwise show
up as a silently wrong metric.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final

from plummy.application.evaluation.metrics import Fact, normalize_text

MAX_DOCUMENTS: Final = 100  # one listing page (pagination.MAX_LIMIT) holds the whole dataset
MAX_QUESTIONS: Final = 1000
LANGUAGES: Final = frozenset({"ru", "en"})


class DatasetError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class EvalDocument:
    filename: str
    data: bytes


@dataclass(frozen=True, slots=True)
class EvalQuestion:
    id: str
    question: str
    language: str
    tags: tuple[str, ...]
    facts: tuple[Fact, ...]

    @property
    def answerable(self) -> bool:
        return bool(self.facts)


@dataclass(frozen=True, slots=True)
class EvalDataset:
    name: str
    description: str
    documents: tuple[EvalDocument, ...]
    questions: tuple[EvalQuestion, ...]
    fingerprint: str  # SHA-256 of the manifest and every document: one KB per exact dataset


def _str(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DatasetError(f"{where}: expected a non-empty string")
    return value


def parse_dataset(manifest_bytes: bytes, files: Mapping[str, bytes]) -> EvalDataset:
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DatasetError(f"manifest is not valid JSON: {exc}") from None
    if not isinstance(manifest, dict):
        raise DatasetError("manifest must be an object")
    name = _str(manifest.get("name"), "name")
    filenames = manifest.get("documents")
    if not isinstance(filenames, list) or not filenames:
        raise DatasetError("documents: expected a non-empty list of file names")
    if len(filenames) > MAX_DOCUMENTS:
        raise DatasetError(f"documents: at most {MAX_DOCUMENTS}")
    if len(set(filenames)) != len(filenames):
        raise DatasetError("documents: duplicate file name")
    documents = []
    texts: dict[str, str] = {}
    for filename in filenames:
        _str(filename, "documents[]")
        if filename not in files:
            raise DatasetError(f"documents: {filename} is missing")
        documents.append(EvalDocument(filename, files[filename]))
        try:
            texts[filename] = normalize_text(files[filename].decode("utf-8"))
        except UnicodeDecodeError:
            raise DatasetError(f"documents: {filename} is not UTF-8 text") from None

    raw_questions = manifest.get("questions")
    if not isinstance(raw_questions, list) or not raw_questions:
        raise DatasetError("questions: expected a non-empty list")
    if len(raw_questions) > MAX_QUESTIONS:
        raise DatasetError(f"questions: at most {MAX_QUESTIONS}")
    questions = []
    seen: set[str] = set()
    for raw in raw_questions:
        if not isinstance(raw, dict):
            raise DatasetError("questions[]: expected an object")
        question_id = _str(raw.get("id"), "questions[].id")
        if question_id in seen:
            raise DatasetError(f"{question_id}: duplicate id")
        seen.add(question_id)
        language = raw.get("language")
        if language not in LANGUAGES:
            raise DatasetError(f"{question_id}: language must be one of {sorted(LANGUAGES)}")
        tags = raw.get("tags", [])
        if not isinstance(tags, list) or not all(isinstance(tag, str) and tag for tag in tags):
            raise DatasetError(f"{question_id}: tags must be a list of strings")
        facts = []
        for item in raw.get("relevant", []):
            if not isinstance(item, dict):
                raise DatasetError(f"{question_id}: relevant[] must be objects")
            document = _str(item.get("document"), f"{question_id}.document")
            evidence = _str(item.get("evidence"), f"{question_id}.evidence")
            if document not in texts:
                raise DatasetError(f"{question_id}: {document} is not in the dataset")
            if normalize_text(evidence) not in texts[document]:
                raise DatasetError(f"{question_id}: the evidence does not occur in {document}")
            facts.append(Fact(document, evidence))
        questions.append(
            EvalQuestion(
                id=question_id,
                question=_str(raw.get("question"), f"{question_id}.question"),
                language=language,
                tags=tuple(tags),
                facts=tuple(facts),
            )
        )
    digest = hashlib.sha256(manifest_bytes)
    for item in documents:
        digest.update(item.filename.encode())
        digest.update(hashlib.sha256(item.data).digest())
    return EvalDataset(
        name=name,
        description=str(manifest.get("description", "")),
        documents=tuple(documents),
        questions=tuple(questions),
        fingerprint=digest.hexdigest(),
    )


def tags_of(questions: Sequence[EvalQuestion]) -> list[str]:
    return sorted({tag for question in questions for tag in question.tags} | {"all"})
