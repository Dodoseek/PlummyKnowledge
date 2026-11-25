"""Retrieval evaluation run (``python -m plummy.cli eval run``, docs/evaluation/).

The dataset goes through the product path: a dedicated knowledge base, ``UploadDocument`` for every file, the
ingestion and indexing workers, then ``RetrieveChunks`` with the scope of an authorized operator. Nothing is
shortcut, so the numbers describe what users get. The knowledge base is named after the dataset fingerprint
and the embedding profile, so a repeated run reuses it and uploads only what is missing.

The report has metrics, ranks and file names; it holds no document text and no question text.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Final
from uuid import UUID, uuid4

from plummy.application.documents.upload import UploadDocument
from plummy.application.errors import ApplicationError, NotFoundError, RateLimitedError
from plummy.application.evaluation.dataset import EvalDataset, EvalQuestion, tags_of
from plummy.application.evaluation.metrics import (
    RetrievedChunk,
    mean,
    ndcg_at,
    new_facts_per_rank,
    recall_at,
    reciprocal_rank,
)
from plummy.application.knowledge.access import require_kb_access
from plummy.application.knowledge.use_cases import CreateKnowledgeBase, KnowledgeBaseInput
from plummy.application.pagination import MAX_LIMIT, PageRequest, SortSpec
from plummy.application.ports import Clock, DocumentQuery, UnitOfWorkFactory
from plummy.application.retrieval.model import (
    DEFAULT_KEYWORD_WEIGHT,
    DEFAULT_RRF_K,
    DEFAULT_VECTOR_WEIGHT,
    RetrievalScope,
    RetrievalSettings,
    SearchMode,
)
from plummy.application.retrieval.use_cases import RetrieveChunks, normalize_query, resolve_scope
from plummy.application.security import ClientInfo, SecurityContext
from plummy.domain.documents import DocumentStatus
from plummy.domain.identity.models import UserStatus
from plummy.domain.knowledge import KbAccessLevel

DEFAULT_KS: Final = (1, 3, 5, 10)
POLL_SECONDS: Final = 2.0
_BY_FILENAME: Final = SortSpec("filename", "filename", False, str)

Progress = Callable[[str], None]
Sleep = Callable[[float], Awaitable[None]]


class EvaluationError(ApplicationError):
    """The run cannot produce trustworthy numbers (documents failed, indexing timed out)."""

    code = "evaluation_failed"


@dataclass(frozen=True, slots=True)
class EvalOptions:
    embedding_profile_id: UUID
    modes: tuple[SearchMode, ...] = (SearchMode.HYBRID, SearchMode.VECTOR, SearchMode.KEYWORD)
    ks: tuple[int, ...] = DEFAULT_KS
    indexing_timeout_seconds: float = 1800.0
    rrf_k: int = DEFAULT_RRF_K
    vector_weight: float = DEFAULT_VECTOR_WEIGHT
    keyword_weight: float = DEFAULT_KEYWORD_WEIGHT

    def settings(self) -> RetrievalSettings:
        # The budget is lifted so that the ranking itself is measured; the default budget is reported apart.
        top = max(self.ks)
        return RetrievalSettings(
            top_k=top,
            vector_top_k=max(40, top),
            keyword_top_k=max(40, top),
            rrf_k=self.rrf_k,
            vector_weight=self.vector_weight,
            keyword_weight=self.keyword_weight,
            max_context_tokens=128_000,
        )


@dataclass(slots=True)
class QuestionResult:
    id: str
    tags: tuple[str, ...]
    answerable: bool
    recall: dict[int, float] = field(default_factory=dict)
    reciprocal_rank: float | None = None
    ndcg: dict[int, float] = field(default_factory=dict)
    ranked_files: list[str] = field(default_factory=list)
    first_relevant_rank: int | None = None
    duration_ms: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tags": list(self.tags),
            "answerable": self.answerable,
            "recall": {str(k): round(v, 4) for k, v in self.recall.items()},
            "reciprocal_rank": None if self.reciprocal_rank is None else round(self.reciprocal_rank, 4),
            "ndcg": {str(k): round(v, 4) for k, v in self.ndcg.items()},
            "first_relevant_rank": self.first_relevant_rank,
            "ranked_files": self.ranked_files,
            "duration_ms": self.duration_ms,
        }


def summarize(results: Sequence[QuestionResult], ks: Sequence[int], tags: Sequence[str]) -> dict[str, Any]:
    """Means over answerable questions, overall and per tag."""
    summary: dict[str, Any] = {}
    for tag in tags:
        group = [r for r in results if r.answerable and (tag == "all" or tag in r.tags)]
        if not group:
            continue
        summary[tag] = {
            "questions": len(group),
            "recall": {str(k): mean([r.recall[k] for r in group]) for k in ks},
            "mrr": mean([r.reciprocal_rank or 0.0 for r in group]),
            "ndcg": {str(k): mean([r.ndcg[k] for r in group]) for k in ks},
        }
    return summary


def score_question(
    question: EvalQuestion, chunks: Sequence[RetrievedChunk], ks: Sequence[int]
) -> QuestionResult:
    result = QuestionResult(question.id, question.tags, question.answerable)
    result.ranked_files = [chunk.filename for chunk in chunks]
    if not question.answerable:
        return result
    gains = new_facts_per_rank(chunks, question.facts)
    relevant = len(question.facts)
    result.recall = {k: recall_at(k, gains, relevant) for k in ks}
    result.ndcg = {k: ndcg_at(k, gains, relevant) for k in ks}
    result.reciprocal_rank = reciprocal_rank(gains)
    result.first_relevant_rank = next((rank for rank, gain in enumerate(gains, start=1) if gain), None)
    return result


async def operator_context(uow: UnitOfWorkFactory, clock: Clock, email: str) -> SecurityContext:
    """The context of an active user for a server-side command: same roles and permissions as their
    sessions, so every use case checks access exactly as for a web request. There is no session; the id is
    a fresh marker that matches none."""
    async with uow() as work:
        user = await work.users.get_by_email(email)
        if user is None or user.status is not UserStatus.ACTIVE:
            raise NotFoundError("No active user with this email.")
        roles, permissions = await work.roles.roles_and_permissions(user.id)
    now = clock.now()
    return SecurityContext(
        user_id=user.id,
        session_id=uuid4(),
        email=user.email,
        display_name=user.display_name,
        roles=roles,
        permissions=permissions,
        session_expires_at=now + timedelta(hours=12),
        session_idle_expires_at=now + timedelta(hours=12),
    )


class RunRetrievalEvaluation:
    def __init__(
        self,
        uow: UnitOfWorkFactory,
        create_kb: CreateKnowledgeBase,
        upload: UploadDocument,
        retrieve: RetrieveChunks,
        clock: Clock,
        *,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._uow, self._create_kb, self._upload, self._retrieve = uow, create_kb, upload, retrieve
        self._clock, self._sleep = clock, sleep

    async def execute(
        self,
        context: SecurityContext,
        dataset: EvalDataset,
        options: EvalOptions,
        progress: Progress = lambda _: None,
    ) -> dict[str, Any]:
        started = self._clock.now()
        kb_id = await self._knowledge_base(context, dataset, options, progress)
        await self._upload_missing(context, kb_id, dataset, progress)
        await self._wait_indexed(context, kb_id, dataset, options, progress)
        async with self._uow() as uow:
            scope = await resolve_scope(uow, context, [kb_id])
        settings = options.settings()
        tags = tags_of(dataset.questions)
        runs = {}
        for mode in options.modes:
            progress(f"retrieval: {mode.value}")
            results = [
                await self._ask(scope, question, settings, mode, options.ks) for question in dataset.questions
            ]
            runs[mode.value] = {
                "summary": summarize(results, options.ks, tags),
                "mean_duration_ms": mean([float(r.duration_ms) for r in results]),
                "questions": [r.to_dict() for r in results],
            }
        return {
            "dataset": {
                "name": dataset.name,
                "fingerprint": dataset.fingerprint,
                "documents": len(dataset.documents),
                "questions": len(dataset.questions),
                "answerable": sum(1 for q in dataset.questions if q.answerable),
                "unanswerable": sum(1 for q in dataset.questions if not q.answerable),
            },
            "knowledge_base_id": str(kb_id),
            "embedding_profile": {
                "id": str(scope.profile.id),
                "name": scope.profile.name,
                "model_id": scope.profile.model_id,
                "dimensions": scope.profile.dimensions,
                "distance_metric": str(scope.profile.distance_metric),
            },
            "settings": {
                "ks": list(options.ks),
                "top_k": settings.top_k,
                "vector_top_k": settings.vector_top_k,
                "keyword_top_k": settings.keyword_top_k,
                "rrf_k": settings.rrf_k,
                "vector_weight": settings.vector_weight,
                "keyword_weight": settings.keyword_weight,
                "max_context_tokens": settings.max_context_tokens,
            },
            "started_at": started.isoformat(),
            "finished_at": self._clock.now().isoformat(),
            "runs": runs,
        }

    async def _ask(
        self,
        scope: RetrievalScope,
        question: EvalQuestion,
        settings: RetrievalSettings,
        mode: SearchMode,
        ks: Sequence[int],
    ) -> QuestionResult:
        began = time.monotonic()
        result = await self._retrieve.search(scope, normalize_query(question.question), settings, mode)
        chunks = [RetrievedChunk(item.hit.filename, item.hit.content) for item in result.chunks]
        scored = score_question(question, chunks, ks)
        scored.duration_ms = round((time.monotonic() - began) * 1000)
        return scored

    async def _knowledge_base(
        self, context: SecurityContext, dataset: EvalDataset, options: EvalOptions, progress: Progress
    ) -> UUID:
        name = f"Evaluation {dataset.name} {dataset.fingerprint[:12]} {options.embedding_profile_id.hex[:8]}"
        async with self._uow() as uow:
            existing = await uow.knowledge_bases.get_by_name(name)
            if existing is not None:
                kb, _ = await require_kb_access(uow, context, existing.id, KbAccessLevel.MANAGE)
                progress(f"knowledge base: reusing {kb.id}")
                return kb.id
        view = await self._create_kb.execute(
            context,
            ClientInfo(user_agent="plummy-cli eval"),
            KnowledgeBaseInput(
                name=name,
                description=f"Retrieval evaluation dataset {dataset.name}. Synthetic documents.",
                embedding_profile_id=options.embedding_profile_id,
            ),
        )
        progress(f"knowledge base: created {view.knowledge_base.id}")
        return view.knowledge_base.id

    async def _documents(
        self, context: SecurityContext, kb_id: UUID
    ) -> dict[str, tuple[UUID, DocumentStatus]]:
        async with self._uow() as uow:
            kb, _ = await require_kb_access(uow, context, kb_id, KbAccessLevel.MANAGE)
            sources = tuple(source.id for source in await uow.data_sources.linked_to(kb.id))
            if not sources:
                raise NotFoundError("The evaluation knowledge base has no source.")
            page = await uow.documents.page(DocumentQuery(sources), PageRequest(_BY_FILENAME, MAX_LIMIT))
        return {doc.filename: (doc.id, doc.processing_status) for doc in page.items}

    async def _upload_missing(
        self, context: SecurityContext, kb_id: UUID, dataset: EvalDataset, progress: Progress
    ) -> None:
        present = await self._documents(context, kb_id)
        missing = [document for document in dataset.documents if document.filename not in present]
        for number, document in enumerate(missing, start=1):
            while True:
                try:
                    await self._upload.execute(
                        context, ClientInfo(user_agent="plummy-cli eval"), kb_id, document.filename,
                        _chunks(document.data),
                    )  # fmt: skip
                    break
                except RateLimitedError as exc:  # the per-user upload limit applies to the CLI too
                    progress(f"upload: rate limited, waiting {exc.retry_after_seconds}s")
                    await self._sleep(exc.retry_after_seconds)
            progress(f"upload: {number}/{len(missing)}")

    async def _wait_indexed(
        self,
        context: SecurityContext,
        kb_id: UUID,
        dataset: EvalDataset,
        options: EvalOptions,
        progress: Progress,
    ) -> None:
        deadline: datetime | None = None
        names = {document.filename for document in dataset.documents}
        last = ""
        while True:
            documents = {
                name: state
                for name, state in (await self._documents(context, kb_id)).items()
                if name in names
            }
            failed = sorted(
                name for name, (_, status) in documents.items() if status is DocumentStatus.FAILED
            )
            if failed:
                raise EvaluationError(f"Documents failed to index: {', '.join(failed)}.")
            waiting = len(names) - sum(1 for _, s in documents.values() if s is DocumentStatus.INDEXED)
            if waiting == 0:
                progress("indexing: done")
                return
            status = f"indexing: {waiting} of {len(names)} documents not indexed yet"
            if status != last:
                progress(status)
                last = status
            now = self._clock.now()
            if deadline is None:
                deadline = now + timedelta(seconds=options.indexing_timeout_seconds)
            elif now >= deadline:
                raise EvaluationError(
                    "Indexing did not finish in time. Are the worker and worker-ingestion services running?"
                )
            await self._sleep(POLL_SECONDS)


async def _chunks(data: bytes) -> AsyncIterator[bytes]:
    yield data
