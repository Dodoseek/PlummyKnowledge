"""Hybrid retrieval (ARCHITECTURE.md §9.4, ADR-017, ADR-018).

Order of the checks is the security property (§8 п.2): the knowledge bases are authorized and their sources
resolved from the verified session **before** any search; the searches filter by those sources in SQL; the
result is fused, deduplicated and cut to the context budget. Query text is never logged.
"""

from __future__ import annotations

import logging
import time
import unicodedata
from collections.abc import Iterable
from typing import Final
from uuid import UUID

from plummy.application.ai.embedding import (
    EmbeddingSetupError,
    InvalidVectorError,
    checked_vector,
    embedding_provider_for,
)
from plummy.application.ai.ports import (
    CancellationToken,
    ProviderEgressDeniedError,
    ProviderError,
    ProviderGateway,
    ProviderTimeoutError,
)
from plummy.application.errors import (
    ConflictError,
    InvalidInputError,
    RateLimitedError,
    ServiceUnavailableError,
    UpstreamEgressDeniedError,
    UpstreamProviderError,
    UpstreamTimeoutError,
)
from plummy.application.knowledge.access import require_kb_access
from plummy.application.ports import RateLimiter, RateLimiterUnavailableError, UnitOfWork, UnitOfWorkFactory
from plummy.application.retrieval.fusion import deduplicate, fit_budget, reciprocal_rank_fusion
from plummy.application.retrieval.model import (
    DEFAULT_TOP_K,
    MAX_KNOWLEDGE_BASES,
    MAX_QUERY_LENGTH,
    ChunkHit,
    RetrievalResult,
    RetrievalScope,
    RetrievalSettings,
    SearchMode,
)
from plummy.application.secret_storage.vault import SecretVault
from plummy.application.security import SecurityContext
from plummy.domain.ai import EmbeddingProfileStatus
from plummy.domain.knowledge import KbAccessLevel, KnowledgeBase

logger = logging.getLogger(__name__)

SEARCHES_PER_MINUTE: Final = 30  # API_CONTRACTS.md §5 ``search`` [Proposed]: one embedding call each


class EmbeddingUnavailableError(ConflictError):
    """The knowledge base cannot be searched by meaning now (profile retired, provider disabled, no key)."""

    code = "embedding_unavailable"


def normalize_query(query: str) -> str:
    """NFC, control characters replaced by spaces, whitespace collapsed; 1-1000 characters remain."""
    cleaned = "".join(
        " " if unicodedata.category(char).startswith("C") else char
        for char in unicodedata.normalize("NFC", query)
    )
    text = " ".join(cleaned.split())
    if not text:
        raise InvalidInputError("query", "empty_query", "The search query is empty.")
    if len(text) > MAX_QUERY_LENGTH:
        raise InvalidInputError(
            "query", "query_too_long", f"The search query is longer than {MAX_QUERY_LENGTH} characters."
        )
    return text


async def resolve_scope(
    uow: UnitOfWork, context: SecurityContext, knowledge_base_ids: Iterable[UUID]
) -> RetrievalScope:
    """The authorized knowledge bases and their sources. Each one needs ``read`` (else ``404``, as for a
    missing one); all must share one embedding profile, since a query vector belongs to one profile."""
    ids = sorted(set(knowledge_base_ids))
    if not ids:
        raise InvalidInputError("knowledge_base_ids", "required", "At least one knowledge base is required.")
    if len(ids) > MAX_KNOWLEDGE_BASES:
        raise InvalidInputError(
            "knowledge_base_ids",
            "too_many",
            f"At most {MAX_KNOWLEDGE_BASES} knowledge bases can be searched together.",
        )
    knowledge_bases: list[KnowledgeBase] = []
    for kb_id in ids:
        kb, _ = await require_kb_access(uow, context, kb_id, KbAccessLevel.READ)
        knowledge_bases.append(kb)
    return await scope_of(uow, knowledge_bases)


async def scope_of(uow: UnitOfWork, knowledge_bases: list[KnowledgeBase]) -> RetrievalScope:
    """Sources and profile of knowledge bases the caller **already authorized**."""
    profile_ids = {kb.embedding_profile_id for kb in knowledge_bases}
    if len(profile_ids) != 1:
        raise InvalidInputError(
            "knowledge_base_ids",
            "mixed_embedding_profiles",
            "The knowledge bases use different embedding profiles and cannot be searched together.",
        )
    profile = await uow.embedding_profiles.get(profile_ids.pop())
    if profile is None or profile.status is EmbeddingProfileStatus.RETIRED:
        raise EmbeddingUnavailableError("The embedding profile of the knowledge base is retired.")
    sources: set[UUID] = set()
    for kb in knowledge_bases:
        sources.update(source.id for source in await uow.data_sources.linked_to(kb.id))
    return RetrievalScope(frozenset(kb.id for kb in knowledge_bases), frozenset(sources), profile)


class RetrieveChunks:
    def __init__(self, uow: UnitOfWorkFactory, gateway: ProviderGateway, vault: SecretVault) -> None:
        self._uow, self._gateway, self._vault = uow, gateway, vault

    async def execute(
        self,
        context: SecurityContext,
        knowledge_base_ids: Iterable[UUID],
        query: str,
        *,
        settings: RetrievalSettings | None = None,
        mode: SearchMode = SearchMode.HYBRID,
    ) -> RetrievalResult:
        text = normalize_query(query)
        async with self._uow() as uow:
            scope = await resolve_scope(uow, context, knowledge_base_ids)
        return await self.search(scope, text, settings or RetrievalSettings(), mode)

    async def search(
        self, scope: RetrievalScope, query: str, settings: RetrievalSettings, mode: SearchMode
    ) -> RetrievalResult:
        """``scope`` must come from :func:`resolve_scope` (or :func:`scope_of` after an equivalent check)."""
        started = time.monotonic()
        vector_hits: list[ChunkHit] = []
        keyword_hits: list[ChunkHit] = []
        keyword_empty = False
        if mode in (SearchMode.HYBRID, SearchMode.VECTOR):
            vector = await self._embed(scope, query)
            async with self._uow() as uow:
                vector_hits = await uow.search.by_vector(scope, vector, limit=settings.vector_top_k)
        if mode in (SearchMode.HYBRID, SearchMode.KEYWORD):
            async with self._uow() as uow:
                found = await uow.search.by_keywords(scope, query, limit=settings.keyword_top_k)
            keyword_empty = found is None
            keyword_hits = found or []
        fused = reciprocal_rank_fusion(
            vector_hits,
            keyword_hits,
            k=settings.rrf_k,
            vector_weight=settings.vector_weight,
            keyword_weight=settings.keyword_weight,
        )
        ranked = deduplicate(fused)
        selection = fit_budget(ranked, top_k=settings.top_k, max_tokens=settings.max_context_tokens)
        logger.info(
            "retrieval finished",
            extra={
                "mode": mode.value,
                "knowledge_bases": len(scope.knowledge_base_ids),
                "vector_hits": len(vector_hits),
                "keyword_hits": len(keyword_hits),
                "chunks": len(selection.chunks),
                "duration_ms": round((time.monotonic() - started) * 1000),
            },
        )
        return RetrievalResult(
            chunks=selection.chunks,
            mode=mode,
            total_tokens=selection.total_tokens,
            vector_candidates=len(vector_hits),
            keyword_candidates=len(keyword_hits),
            keyword_query_empty=keyword_empty,
            skipped_over_budget=selection.skipped_over_budget,
        )

    async def _embed(self, scope: RetrievalScope, query: str) -> tuple[float, ...]:
        profile = scope.profile
        async with self._uow() as uow:
            try:
                provider = await embedding_provider_for(uow, self._vault, self._gateway, profile)
            except EmbeddingSetupError as exc:
                raise EmbeddingUnavailableError(exc.message) from None
        try:
            vectors = await provider.embed([query], model_id=profile.model_id, cancel=CancellationToken())
        except ProviderTimeoutError as exc:
            raise UpstreamTimeoutError(exc.code.value) from None
        except ProviderEgressDeniedError as exc:
            raise UpstreamEgressDeniedError(exc.code.value) from None
        except ProviderError as exc:
            logger.warning("query embedding failed", extra={"provider_error": exc.code.value})
            raise UpstreamProviderError(exc.code.value) from None
        if len(vectors) != 1:
            raise UpstreamProviderError("embedding_invalid_response")
        try:
            return checked_vector(vectors[0], profile)
        except InvalidVectorError as exc:
            raise UpstreamProviderError(exc.code) from None


class SearchKnowledgeBase:
    """``POST /knowledge-bases/{id}/search``: retrieval without generation, for people with ``read``.

    The knowledge base is authorized first (``404`` without access, nothing counted), then the per-user
    ``search`` limit is taken, then the query is embedded: a refused caller never reaches the provider.
    """

    def __init__(self, uow: UnitOfWorkFactory, retrieve: RetrieveChunks, limiter: RateLimiter) -> None:
        self._uow, self._retrieve, self._limiter = uow, retrieve, limiter

    async def execute(
        self,
        context: SecurityContext,
        knowledge_base_id: UUID,
        query: str,
        *,
        mode: SearchMode = SearchMode.HYBRID,
        top_k: int = DEFAULT_TOP_K,
    ) -> RetrievalResult:
        text = normalize_query(query)
        try:
            settings = RetrievalSettings(top_k=top_k)
        except ValueError:
            raise InvalidInputError("top_k", "out_of_range", "top_k must be between 1 and 50.") from None
        async with self._uow() as uow:
            scope = await resolve_scope(uow, context, [knowledge_base_id])
        try:
            status = await self._limiter.hit(
                f"search:user:{context.user_id.hex}:1m", limit=SEARCHES_PER_MINUTE, window_seconds=60
            )
        except RateLimiterUnavailableError:
            raise ServiceUnavailableError() from None
        if not status.allowed:
            raise RateLimitedError(status.retry_after_seconds)
        return await self._retrieve.search(scope, text, settings, mode)
