"""Knowledge bases (API_CONTRACTS.md §2.10, DATABASE_SCHEMA.md §5.1-5.4, ADR-008).

A knowledge base is created with an embedding profile and its own ``upload`` document source. Grants and other
sources are managed in phase 7; until then the knowledge bases are visible to ``knowledge_bases.manage``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from plummy.application.audit import by_user
from plummy.application.errors import InvalidInputError, KnowledgeBaseNameTakenError
from plummy.application.knowledge.access import effective_access, require_kb_access
from plummy.application.pagination import Page, PageRequest
from plummy.application.ports import (
    Clock,
    IndexingStats,
    JobQueue,
    JobQueueUnavailableError,
    UniqueViolationError,
    UnitOfWorkFactory,
)
from plummy.application.security import ClientInfo, SecurityContext
from plummy.core.ids import new_id
from plummy.domain.ai import MAX_DIMENSIONS, EmbeddingProfileStatus
from plummy.domain.audit import AuditAction
from plummy.domain.identity import Permission
from plummy.domain.knowledge import (
    DataSource,
    DataSourceStatus,
    DataSourceType,
    InvalidKnowledgeInputError,
    KbAccessLevel,
    KnowledgeBase,
    normalize_description,
    normalize_kb_name,
    upload_source_name,
)

logger = logging.getLogger(__name__)

NAME_TAKEN: Final = "A knowledge base with this name already exists."
PROFILE_FIELD: Final = "embedding_profile_id"


@dataclass(frozen=True, slots=True)
class KnowledgeBaseInput:
    name: str
    embedding_profile_id: UUID
    description: str | None = None


@dataclass(frozen=True, slots=True)
class KnowledgeBaseView:
    knowledge_base: KnowledgeBase
    access_level: KbAccessLevel
    indexing: IndexingStats


class ListKnowledgeBases:
    """The knowledge bases the caller can read (all of them with ``knowledge_bases.manage``)."""

    def __init__(self, uow: UnitOfWorkFactory) -> None:
        self._uow = uow

    async def execute(self, context: SecurityContext, page: PageRequest) -> Page[KnowledgeBaseView]:
        sees_all = context.has_permission(Permission.KNOWLEDGE_BASES_MANAGE)
        async with self._uow() as uow:
            result = await uow.knowledge_bases.page(page, visible_to=None if sees_all else context.user_id)
            ids = [kb.id for kb in result.items]
            levels = await effective_access(uow, context, ids)
            stats = await uow.knowledge_bases.indexing_stats(ids)
        return Page(
            items=[
                KnowledgeBaseView(kb, levels[kb.id], stats.get(kb.id, IndexingStats()))
                for kb in result.items
                if kb.id in levels
            ],
            next_cursor=result.next_cursor,
        )


class GetKnowledgeBase:
    def __init__(self, uow: UnitOfWorkFactory) -> None:
        self._uow = uow

    async def execute(self, context: SecurityContext, kb_id: UUID) -> KnowledgeBaseView:
        async with self._uow() as uow:
            kb, level = await require_kb_access(uow, context, kb_id, KbAccessLevel.READ)
            stats = await uow.knowledge_bases.indexing_stats([kb.id])
        return KnowledgeBaseView(kb, level, stats.get(kb.id, IndexingStats()))


class CreateKnowledgeBase:
    def __init__(self, uow: UnitOfWorkFactory, clock: Clock, jobs: JobQueue) -> None:
        self._uow, self._clock, self._jobs = uow, clock, jobs

    async def execute(
        self, context: SecurityContext, client: ClientInfo, data: KnowledgeBaseInput
    ) -> KnowledgeBaseView:
        context.require(Permission.KNOWLEDGE_BASES_MANAGE)
        try:
            name = normalize_kb_name(data.name)
            description = normalize_description(data.description)
        except InvalidKnowledgeInputError as exc:
            raise InvalidInputError(exc.field, exc.code, str(exc)) from None
        now = self._clock.now()
        kb = KnowledgeBase(
            id=new_id(),
            name=name,
            description=description,
            embedding_profile_id=data.embedding_profile_id,
            created_by=context.user_id,
            created_at=now,
            updated_at=now,
        )
        source = DataSource(
            id=new_id(),
            type=DataSourceType.UPLOAD,
            name=upload_source_name(name),
            status=DataSourceStatus.ACTIVE,
            owner_knowledge_base_id=kb.id,
            created_by=context.user_id,
            created_at=now,
            updated_at=now,
        )
        async with self._uow() as uow:
            profile = await uow.embedding_profiles.get(data.embedding_profile_id)
            if profile is None:
                raise InvalidInputError(
                    PROFILE_FIELD, "unknown_embedding_profile", "The embedding profile does not exist."
                )
            if profile.status is not EmbeddingProfileStatus.ACTIVE:
                raise InvalidInputError(
                    PROFILE_FIELD, "embedding_profile_not_active", "The embedding profile is not active."
                )
            if profile.dimensions > MAX_DIMENSIONS:
                # Profiles created before ADR-017 may be larger than an HNSW index allows.
                raise InvalidInputError(
                    PROFILE_FIELD,
                    "dimensions_not_indexable",
                    f"Vectors of more than {MAX_DIMENSIONS} dimensions cannot be indexed.",
                )
            if await uow.providers.get(profile.provider_id) is None:
                raise InvalidInputError(
                    PROFILE_FIELD,
                    "provider_unavailable",
                    "The provider of the embedding profile was deleted.",
                )
            if await uow.knowledge_bases.get_by_name(name) is not None:
                raise KnowledgeBaseNameTakenError(NAME_TAKEN)
            try:
                await uow.knowledge_bases.add(kb)
            except UniqueViolationError:
                raise KnowledgeBaseNameTakenError(NAME_TAKEN) from None
            await uow.data_sources.add(source)
            await uow.data_sources.attach(kb.id, source.id, at=now)
            await uow.audit.record(
                by_user(
                    context,
                    client,
                    AuditAction.KB_CREATED,
                    target_type="knowledge_base",
                    target_id=kb.id,
                    details={"embedding_profile_id": str(profile.id), "upload_source_id": str(source.id)},
                )
            )
            await uow.commit()
        # The profile's HNSW index is built before the first vectors (ADR-017 §2). Idempotent; if the broker
        # is down now, the index phase of the first document asks again.
        try:
            await self._jobs.enqueue_vector_index(profile.id)
        except JobQueueUnavailableError:
            logger.warning("vector index request not sent", extra={"embedding_profile_id": str(profile.id)})
        return KnowledgeBaseView(kb, KbAccessLevel.MANAGE, IndexingStats())
