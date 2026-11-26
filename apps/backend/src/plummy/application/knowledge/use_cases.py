"""Knowledge bases (API_CONTRACTS.md §2.10, DATABASE_SCHEMA.md §5.1-5.4, ADR-008).

A knowledge base is created with an embedding profile and its own ``upload`` document source. Its access
list (users and groups with ``read`` or ``manage``) is replaced as a whole; deleting it soft-deletes its
documents in the same transaction (they leave retrieval at once) and purges them afterwards.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from plummy.application.audit import by_user
from plummy.application.errors import InvalidInputError, KnowledgeBaseNameTakenError, NotFoundError
from plummy.application.knowledge.access import KB_NOT_FOUND, effective_access, require_kb_access
from plummy.application.pagination import Page, PageRequest
from plummy.application.ports import (
    Clock,
    IndexingStats,
    JobQueue,
    JobQueueUnavailableError,
    UniqueViolationError,
    UnitOfWork,
    UnitOfWorkFactory,
)
from plummy.application.secret_storage.vault import SecretVault
from plummy.application.security import ClientInfo, SecurityContext
from plummy.application.sources.ports import OAuthClients
from plummy.application.sources.use_cases import drop_credentials, revoke_token
from plummy.core.ids import new_id
from plummy.domain.ai import MAX_DIMENSIONS, EmbeddingProfileStatus
from plummy.domain.audit import AuditAction, AuditEntry
from plummy.domain.documents import DeletionReason
from plummy.domain.identity import Permission
from plummy.domain.knowledge import (
    MAX_KB_GRANTS,
    DataSource,
    DataSourceStatus,
    DataSourceType,
    InvalidKnowledgeInputError,
    KbAccessLevel,
    KbGrant,
    KnowledgeBase,
    PrincipalType,
    normalize_description,
    normalize_kb_name,
    upload_source_name,
)
from plummy.domain.secrets import SecretValue

logger = logging.getLogger(__name__)

NAME_TAKEN: Final = "A knowledge base with this name already exists."
PROFILE_FIELD: Final = "embedding_profile_id"
CANCELLED_BY_KB_DELETION: Final = "kb_deleted"


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


def _normalized(name: str | None, description: str | None) -> tuple[str | None, str | None]:
    try:
        return (
            normalize_kb_name(name) if name is not None else None,
            normalize_description(description),
        )
    except InvalidKnowledgeInputError as exc:
        raise InvalidInputError(exc.field, exc.code, str(exc)) from None


@dataclass(frozen=True, slots=True)
class KnowledgeBaseChanges:
    """``PATCH``: only the fields that were sent. ``description=None`` with ``set_description`` clears it."""

    name: str | None = None
    description: str | None = None
    set_description: bool = False
    embedding_profile_id: UUID | None = None


class UpdateKnowledgeBase:
    """Name and description. A different embedding profile would need every document embedded again: until
    that reindex exists the profile is fixed (``422 embedding_profile_change_not_supported``)."""

    def __init__(self, uow: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def execute(
        self, context: SecurityContext, client: ClientInfo, kb_id: UUID, changes: KnowledgeBaseChanges
    ) -> KnowledgeBaseView:
        name, description = _normalized(changes.name, changes.description)
        now = self._clock.now()
        async with self._uow() as uow:
            _, level = await require_kb_access(uow, context, kb_id, KbAccessLevel.MANAGE)
            kb = await uow.knowledge_bases.get_for_update(kb_id)
            if kb is None:  # deleted after the access check
                raise NotFoundError(KB_NOT_FOUND)
            if changes.embedding_profile_id not in (None, kb.embedding_profile_id):
                raise InvalidInputError(
                    PROFILE_FIELD,
                    "embedding_profile_change_not_supported",
                    "The embedding profile of a knowledge base cannot be changed yet.",
                )
            changed: list[str] = []
            if name is not None and name != kb.name:
                if (other := await uow.knowledge_bases.get_by_name(name)) is not None and other.id != kb.id:
                    raise KnowledgeBaseNameTakenError(NAME_TAKEN)
                kb.name = name
                changed.append("name")
            if changes.set_description and description != kb.description:
                kb.description = description
                changed.append("description")
            if changed:
                try:
                    await uow.knowledge_bases.update(kb, at=now)
                except UniqueViolationError:
                    raise KnowledgeBaseNameTakenError(NAME_TAKEN) from None
                kb.updated_at = now
                if "name" in changed and (source := await uow.data_sources.upload_source_of(kb.id)):
                    await uow.data_sources.rename(source.id, upload_source_name(kb.name), at=now)
                await uow.audit.record(
                    by_user(
                        context,
                        client,
                        AuditAction.KB_UPDATED,
                        target_type="knowledge_base",
                        target_id=kb.id,
                        details={"fields": ",".join(changed)},
                    )
                )
                await uow.commit()
        async with self._uow() as uow:
            stats = await uow.knowledge_bases.indexing_stats([kb.id])
        return KnowledgeBaseView(kb, level, stats.get(kb.id, IndexingStats()))


@dataclass(frozen=True, slots=True)
class DeletionResult:
    documents: int
    jobs_cancelled: int


class DeleteKnowledgeBase:
    """``knowledge_bases.manage`` only. One transaction: the knowledge base, its access list and source
    links, its ``upload`` source and every document in it are deleted, their jobs cancelled; files, chunks
    and vectors are purged afterwards (or by the periodic sweep when the broker is down)."""

    def __init__(
        self,
        uow: UnitOfWorkFactory,
        queue: JobQueue,
        clock: Clock,
        vault: SecretVault | None = None,
        clients: OAuthClients | None = None,
    ) -> None:
        self._uow, self._queue, self._clock = uow, queue, clock
        self._vault, self._clients = vault, clients or {}

    async def execute(self, context: SecurityContext, client: ClientInfo, kb_id: UUID) -> DeletionResult:
        context.require(Permission.KNOWLEDGE_BASES_MANAGE)
        now = self._clock.now()
        async with self._uow() as uow:
            kb = await uow.knowledge_bases.get_for_update(kb_id)
            if kb is None:
                raise NotFoundError(KB_NOT_FOUND)
            deletion = await delete_knowledge_base_in(uow, kb, self._vault, now=now)
            documents, cancelled = deletion.documents, deletion.jobs_cancelled
            await uow.audit.record(
                by_user(
                    context,
                    client,
                    AuditAction.KB_DELETED,
                    target_type="knowledge_base",
                    target_id=kb.id,
                    details={"documents": len(documents), "jobs_cancelled": cancelled},
                )
            )
            await uow.commit()
        await finish_knowledge_base_deletion(deletion, self._queue, self._clients, kb_id)
        return DeletionResult(len(documents), cancelled)


@dataclass(frozen=True, slots=True)
class KnowledgeBaseDeletion:
    documents: list[UUID]
    jobs_cancelled: int
    tokens: list[tuple[DataSource, SecretValue | None]]


async def delete_knowledge_base_in(
    uow: UnitOfWork, kb: KnowledgeBase, vault: SecretVault | None, *, now: datetime
) -> KnowledgeBaseDeletion:
    """Delete a locked, live knowledge base inside the caller's transaction: the upload source and every
    external source go with it (ADR-020 §6), external credentials are deleted now (revoked at the provider
    after the commit by :func:`finish_knowledge_base_deletion`). Shared by the API and ``maintenance
    reapply-deletions`` (ADR-023 §6)."""
    tokens: list[tuple[DataSource, SecretValue | None]] = []
    documents: list[UUID] = []
    for source in await uow.data_sources.linked_to(kb.id):
        documents += await uow.documents.mark_deleted_in_source(source.id, DeletionReason.KB_DELETED, at=now)
        if source.is_external:
            if vault is not None:
                _, token = await drop_credentials(uow, vault, source.id)
                tokens.append((source, token))
            await uow.sync_jobs.cancel_active(source.id, error_code="kb_deleted", at=now)
        await uow.data_sources.mark_deleted(source.id, at=now)
    cancelled = await uow.ingestion_jobs.cancel_unfinished(documents, CANCELLED_BY_KB_DELETION, at=now)
    await uow.knowledge_bases.mark_deleted(kb.id, at=now)
    return KnowledgeBaseDeletion(documents, cancelled, tokens)


async def finish_knowledge_base_deletion(
    deletion: KnowledgeBaseDeletion, queue: JobQueue, clients: OAuthClients, kb_id: UUID
) -> None:
    """After the commit: revoke the external tokens, publish the purges (the sweep covers a broker outage)."""
    for source, token in deletion.tokens:
        await revoke_token(clients.get(source.type), token, source.id)
    for document_id in deletion.documents:
        try:
            await queue.enqueue_purge(document_id)
        except JobQueueUnavailableError:
            logger.warning("purge not published, broker unavailable", extra={"knowledge_base_id": str(kb_id)})
            break  # the sweep purges the rest


@dataclass(frozen=True, slots=True)
class GrantView:
    grant: KbGrant
    name: str  # the user's display name or the group's name
    email: str | None  # users only


async def _grant_views(uow: UnitOfWork, grants: Sequence[KbGrant]) -> list[GrantView]:
    views = []
    for grant in grants:
        if grant.principal_type is PrincipalType.USER:
            user = await uow.users.get(grant.principal_id)
            if user is None or user.deleted_at is not None:
                continue  # a deleted user's grant gives nothing; it is dropped on the next replace
            views.append(GrantView(grant, user.display_name, user.email))
        else:
            group = await uow.groups.get(grant.principal_id)
            if group is None:
                continue
            views.append(GrantView(grant, group.name, None))
    return views


class GetKnowledgeBaseAccess:
    def __init__(self, uow: UnitOfWorkFactory) -> None:
        self._uow = uow

    async def execute(self, context: SecurityContext, kb_id: UUID) -> list[GrantView]:
        async with self._uow() as uow:
            await require_kb_access(uow, context, kb_id, KbAccessLevel.MANAGE)
            return await _grant_views(uow, await uow.knowledge_bases.grants(kb_id))


@dataclass(frozen=True, slots=True)
class GrantInput:
    principal_type: PrincipalType
    principal_id: UUID
    access_level: KbAccessLevel


class ReplaceKnowledgeBaseAccess:
    """Replace the access list. Every principal must be a live user or group; each appears once. A caller who
    manages the knowledge base only through the list cannot remove their own ``manage`` (``422
    would_lose_manage``): the check runs on the new list before commit, so a group grant counts too."""

    def __init__(self, uow: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def execute(
        self, context: SecurityContext, client: ClientInfo, kb_id: UUID, entries: Sequence[GrantInput]
    ) -> list[GrantView]:
        if len(entries) > MAX_KB_GRANTS:
            raise InvalidInputError("grants", "too_many", f"At most {MAX_KB_GRANTS} entries.")
        seen: set[tuple[PrincipalType, UUID]] = set()
        for index, entry in enumerate(entries):
            key = (entry.principal_type, entry.principal_id)
            if key in seen:
                raise InvalidInputError(
                    f"grants.{index}.principal_id", "duplicate_principal", "This principal is listed twice."
                )
            seen.add(key)
        now = self._clock.now()
        async with self._uow() as uow:
            await require_kb_access(uow, context, kb_id, KbAccessLevel.MANAGE)
            if await uow.knowledge_bases.get_for_update(kb_id) is None:
                raise NotFoundError(KB_NOT_FOUND)
            for index, entry in enumerate(entries):
                if not await self._exists(uow, entry):
                    raise InvalidInputError(
                        f"grants.{index}.principal_id",
                        "unknown_principal",
                        f"The {entry.principal_type} does not exist.",
                    )
            before = {grant.key: grant for grant in await uow.knowledge_bases.grants(kb_id)}
            wanted = [KbGrant(e.principal_type, e.principal_id, e.access_level) for e in entries]
            await uow.knowledge_bases.replace_grants(kb_id, wanted, granted_by=context.user_id, at=now)
            if not context.has_permission(Permission.KNOWLEDGE_BASES_MANAGE):
                levels = (await uow.knowledge_bases.grants_of_user(context.user_id, [kb_id])).get(kb_id, [])
                if KbAccessLevel.MANAGE not in levels:
                    raise InvalidInputError(
                        "grants",
                        "would_lose_manage",
                        "You cannot remove your own permission to manage this knowledge base.",
                    )
            for grant in wanted:
                old = before.pop(grant.key, None)
                if old is None or old.access_level is not grant.access_level:
                    await uow.audit.record(
                        self._entry(context, client, AuditAction.KB_ACCESS_GRANTED, kb_id, grant)
                    )
            for grant in before.values():
                await uow.audit.record(
                    self._entry(context, client, AuditAction.KB_ACCESS_REVOKED, kb_id, grant)
                )
            await uow.commit()
            return await _grant_views(uow, await uow.knowledge_bases.grants(kb_id))

    @staticmethod
    async def _exists(uow: UnitOfWork, entry: GrantInput) -> bool:
        if entry.principal_type is PrincipalType.USER:
            user = await uow.users.get(entry.principal_id)
            return user is not None and user.deleted_at is None
        return await uow.groups.get(entry.principal_id) is not None

    @staticmethod
    def _entry(
        context: SecurityContext, client: ClientInfo, action: AuditAction, kb_id: UUID, grant: KbGrant
    ) -> AuditEntry:
        return by_user(
            context,
            client,
            action,
            target_type="knowledge_base",
            target_id=kb_id,
            details={
                "principal_type": str(grant.principal_type),
                "principal_id": str(grant.principal_id),
                "access_level": str(grant.access_level),
            },
        )
