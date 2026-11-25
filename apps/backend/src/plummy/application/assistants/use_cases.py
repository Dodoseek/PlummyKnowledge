"""Assistants (API_CONTRACTS.md §2.12, DATABASE_SCHEMA.md §5.3, ADR-005).

``assistants.manage`` creates and configures assistants and sees all of them with their availability;
anyone with ``chat.use`` sees only the assistants they can chat with. Instructions are shown to managers
only and are never written to logs or audit details.

Validation: the chat model must exist and be enabled, the embedding profile must be active, every knowledge
base must use the assistant's embedding profile (one vector space per search) and be readable by the
caller, who cannot attach a knowledge base they do not see. The model and the profile are checked when they
are set or changed, so an unrelated edit (e.g. disabling the assistant) still works after the model was
disabled.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field, replace
from typing import Final
from uuid import UUID

from plummy.application.assistants.access import (
    ASSISTANT_NOT_FOUND,
    Availability,
    availability,
    require_assistant,
)
from plummy.application.audit import by_user
from plummy.application.errors import (
    AssistantNameTakenError,
    InvalidInputError,
    NotFoundError,
    PermissionDeniedError,
)
from plummy.application.knowledge.access import effective_access
from plummy.application.pagination import Page, PageRequest
from plummy.application.ports import Clock, UniqueViolationError, UnitOfWork, UnitOfWorkFactory
from plummy.application.retrieval.model import MAX_KNOWLEDGE_BASES, RetrievalSettings
from plummy.application.security import ClientInfo, SecurityContext
from plummy.core.ids import new_id
from plummy.domain.ai import EmbeddingProfileStatus
from plummy.domain.assistants import (
    DEFAULT_HISTORY_MESSAGES,
    DEFAULT_OUTPUT_TOKENS,
    MAX_HISTORY_MESSAGES,
    MAX_OUTPUT_TOKENS,
    Assistant,
    AssistantRetrieval,
    AssistantStatus,
    InvalidAssistantInputError,
    normalize_instructions,
)
from plummy.domain.audit import AuditAction
from plummy.domain.identity import Permission
from plummy.domain.knowledge import InvalidKnowledgeInputError, normalize_description, normalize_kb_name

logger = logging.getLogger(__name__)

NAME_TAKEN: Final = "An assistant with this name already exists."
LIST_DENIED: Final = "You do not have permission to use assistants."


def default_retrieval() -> AssistantRetrieval:
    defaults = RetrievalSettings()
    return AssistantRetrieval(
        top_k=defaults.top_k,
        vector_top_k=defaults.vector_top_k,
        keyword_top_k=defaults.keyword_top_k,
        rrf_k=defaults.rrf_k,
        vector_weight=defaults.vector_weight,
        keyword_weight=defaults.keyword_weight,
    )


def retrieval_settings(assistant: Assistant) -> RetrievalSettings:
    """What retrieval runs with for this assistant (chat, phase 8)."""
    r = assistant.retrieval
    return RetrievalSettings(
        top_k=r.top_k,
        vector_top_k=r.vector_top_k,
        keyword_top_k=r.keyword_top_k,
        rrf_k=r.rrf_k,
        vector_weight=r.vector_weight,
        keyword_weight=r.keyword_weight,
        max_context_tokens=assistant.max_context_tokens,
    )


@dataclass(frozen=True, slots=True)
class RetrievalChanges:
    top_k: int | None = None
    vector_top_k: int | None = None
    keyword_top_k: int | None = None
    rrf_k: int | None = None
    vector_weight: float | None = None
    keyword_weight: float | None = None

    def apply(self, base: AssistantRetrieval) -> AssistantRetrieval:
        values = {
            name: getattr(self, name) for name in self.__dataclass_fields__ if getattr(self, name) is not None
        }
        return replace(base, **values)


@dataclass(frozen=True, slots=True)
class AssistantChanges:
    """Create and ``PATCH``: only the fields in ``sent`` are applied (``description`` may be cleared with
    ``None``). Create needs ``name``, ``model_config_id`` and ``embedding_profile_id``."""

    sent: frozenset[str]
    name: str | None = None
    description: str | None = None
    instructions: str | None = None
    model_config_id: UUID | None = None
    embedding_profile_id: UUID | None = None
    knowledge_base_ids: tuple[UUID, ...] | None = None
    retrieval: RetrievalChanges = field(default_factory=RetrievalChanges)
    max_context_tokens: int | None = None
    max_history_messages: int | None = None
    max_output_tokens: int | None = None
    status: AssistantStatus | None = None


@dataclass(frozen=True, slots=True)
class AssistantView:
    assistant: Assistant
    availability: Availability
    show_instructions: bool


def _out_of_range(name: str, message: str) -> InvalidInputError:
    return InvalidInputError(name, "out_of_range", message)


def _apply(base: Assistant, changes: AssistantChanges) -> Assistant:
    """``base`` with the sent fields, normalized and range-checked (no lookups)."""
    sent = changes.sent
    updated = replace(base)
    try:
        if "name" in sent and changes.name is not None:
            updated.name = normalize_kb_name(changes.name)
        if "description" in sent:
            updated.description = normalize_description(changes.description)
    except InvalidKnowledgeInputError as exc:
        raise InvalidInputError(exc.field, exc.code, str(exc)) from None
    try:
        if "instructions" in sent and changes.instructions is not None:
            updated.instructions = normalize_instructions(changes.instructions)
    except InvalidAssistantInputError as exc:
        raise InvalidInputError(exc.field, exc.code, str(exc)) from None
    if "model_config_id" in sent and changes.model_config_id is not None:
        updated.model_config_id = changes.model_config_id
    if "embedding_profile_id" in sent and changes.embedding_profile_id is not None:
        updated.embedding_profile_id = changes.embedding_profile_id
    if "knowledge_base_ids" in sent and changes.knowledge_base_ids is not None:
        ids = changes.knowledge_base_ids
        if len(set(ids)) != len(ids):
            raise InvalidInputError(
                "knowledge_base_ids", "duplicate_knowledge_base", "A knowledge base is listed twice."
            )
        if len(ids) > MAX_KNOWLEDGE_BASES:
            raise _out_of_range("knowledge_base_ids", f"At most {MAX_KNOWLEDGE_BASES} knowledge bases.")
        updated.knowledge_base_ids = tuple(sorted(ids))
    if "retrieval" in sent:
        updated.retrieval = changes.retrieval.apply(base.retrieval)
    for name in ("max_context_tokens", "max_history_messages", "max_output_tokens"):
        if name in sent and (value := getattr(changes, name)) is not None:
            setattr(updated, name, value)
    if "status" in sent and changes.status is not None:
        updated.status = changes.status
    try:
        retrieval_settings(updated)
    except ValueError as exc:
        field_name = "limits.max_context_tokens" if "max_context_tokens" in str(exc) else "retrieval"
        raise _out_of_range(field_name, str(exc)) from None
    if not 0 <= updated.max_history_messages <= MAX_HISTORY_MESSAGES:
        raise _out_of_range("limits.max_history_messages", f"Must be 0-{MAX_HISTORY_MESSAGES}.")
    if not 1 <= updated.max_output_tokens <= MAX_OUTPUT_TOKENS:
        raise _out_of_range("limits.max_output_tokens", f"Must be 1-{MAX_OUTPUT_TOKENS}.")
    return updated


async def _check_references(
    uow: UnitOfWork, context: SecurityContext, assistant: Assistant, previous: Assistant | None
) -> None:
    """Lookups: model, profile and knowledge bases, as far as they are new or changed."""
    model_changed = previous is None or previous.model_config_id != assistant.model_config_id
    if model_changed or (previous is not None and previous.max_output_tokens != assistant.max_output_tokens):
        model = await uow.models.find(assistant.model_config_id)
        provider = await uow.providers.get(model.provider_id) if model is not None else None
        if model is None or provider is None or provider.deleted_at is not None:
            raise InvalidInputError("model_config_id", "unknown_model", "The chat model does not exist.")
        if model_changed and not (model.enabled and provider.enabled):
            raise InvalidInputError(
                "model_config_id", "model_disabled", "The chat model or its provider is disabled."
            )
        if model.max_output_tokens is not None and assistant.max_output_tokens > model.max_output_tokens:
            raise InvalidInputError(
                "limits.max_output_tokens",
                "exceeds_model_limit",
                f"The model writes at most {model.max_output_tokens} tokens.",
            )
    if previous is None or previous.embedding_profile_id != assistant.embedding_profile_id:
        profile = await uow.embedding_profiles.get(assistant.embedding_profile_id)
        if profile is None:
            raise InvalidInputError(
                "embedding_profile_id", "unknown_embedding_profile", "The embedding profile does not exist."
            )
        if profile.status is not EmbeddingProfileStatus.ACTIVE:
            raise InvalidInputError(
                "embedding_profile_id", "embedding_profile_not_active", "The embedding profile is not active."
            )
    added = set(assistant.knowledge_base_ids) - set(previous.knowledge_base_ids if previous else ())
    readable = await effective_access(uow, context, sorted(added)) if added else {}
    for kb_id in assistant.knowledge_base_ids:
        kb = await uow.knowledge_bases.get(kb_id)
        if kb is None or (kb_id in added and kb_id not in readable):
            # One the caller cannot read is reported like a missing one (its existence stays hidden).
            raise InvalidInputError(
                "knowledge_base_ids", "unknown_knowledge_base", "A knowledge base does not exist."
            )
        if kb.embedding_profile_id != assistant.embedding_profile_id:
            raise InvalidInputError(
                "knowledge_base_ids",
                "embedding_profile_mismatch",
                "Every knowledge base must use the embedding profile of the assistant.",
            )


def _changed_fields(before: Assistant, after: Assistant) -> list[str]:
    names = (
        "name",
        "description",
        "instructions",
        "model_config_id",
        "embedding_profile_id",
        "knowledge_base_ids",
        "retrieval",
        "max_context_tokens",
        "max_history_messages",
        "max_output_tokens",
        "status",
    )
    return [name for name in names if getattr(before, name) != getattr(after, name)]


class ListAssistants:
    def __init__(self, uow: UnitOfWorkFactory) -> None:
        self._uow = uow

    async def execute(self, context: SecurityContext, page: PageRequest) -> Page[AssistantView]:
        manager = context.has_permission(Permission.ASSISTANTS_MANAGE)
        if not manager and not context.has_permission(Permission.CHAT_USE):
            raise PermissionDeniedError(LIST_DENIED)
        async with self._uow() as uow:
            result = await uow.assistants.page(
                page,
                usable_by=None if manager else context.user_id,
                all_knowledge_bases=context.has_permission(Permission.KNOWLEDGE_BASES_MANAGE),
            )
            states = await availability(uow, context, result.items)
        items = [AssistantView(a, states[a.id], manager) for a in result.items]
        if not manager:
            # Defense in depth: the SQL filter and the policy must agree; nothing unavailable is shown.
            items = [view for view in items if view.availability.available]
        return Page(items=items, next_cursor=result.next_cursor)


class GetAssistant:
    def __init__(self, uow: UnitOfWorkFactory) -> None:
        self._uow = uow

    async def execute(self, context: SecurityContext, assistant_id: UUID) -> AssistantView:
        async with self._uow() as uow:
            assistant, state = await require_assistant(uow, context, assistant_id)
        return AssistantView(assistant, state, context.has_permission(Permission.ASSISTANTS_MANAGE))


class CreateAssistant:
    def __init__(self, uow: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def execute(
        self, context: SecurityContext, client: ClientInfo, changes: AssistantChanges
    ) -> AssistantView:
        context.require(Permission.ASSISTANTS_MANAGE)
        if changes.name is None or changes.model_config_id is None or changes.embedding_profile_id is None:
            raise InvalidInputError(
                "name", "required", "Name, chat model and embedding profile are required."
            )
        now = self._clock.now()
        draft = Assistant(
            id=new_id(),
            name="",
            description=None,
            instructions="",
            model_config_id=changes.model_config_id,
            embedding_profile_id=changes.embedding_profile_id,
            retrieval=default_retrieval(),
            max_context_tokens=RetrievalSettings().max_context_tokens,
            max_history_messages=DEFAULT_HISTORY_MESSAGES,
            max_output_tokens=DEFAULT_OUTPUT_TOKENS,
            status=AssistantStatus.ACTIVE,
            created_by=context.user_id,
            created_at=now,
            updated_at=now,
        )
        assistant = _apply(draft, replace(changes, sent=changes.sent | {"name"}))
        async with self._uow() as uow:
            await _check_references(uow, context, assistant, None)
            if await uow.assistants.get_by_name(assistant.name) is not None:
                raise AssistantNameTakenError(NAME_TAKEN)
            try:
                await uow.assistants.add(assistant)
            except UniqueViolationError:
                raise AssistantNameTakenError(NAME_TAKEN) from None
            await uow.audit.record(
                by_user(
                    context,
                    client,
                    AuditAction.ASSISTANT_CREATED,
                    target_type="assistant",
                    target_id=assistant.id,
                    details={
                        "model_config_id": str(assistant.model_config_id),
                        "knowledge_bases": len(assistant.knowledge_base_ids),
                        "status": str(assistant.status),
                    },
                )
            )
            await uow.commit()
            state = (await availability(uow, context, [assistant]))[assistant.id]
        return AssistantView(assistant, state, True)


class UpdateAssistant:
    def __init__(self, uow: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def execute(
        self, context: SecurityContext, client: ClientInfo, assistant_id: UUID, changes: AssistantChanges
    ) -> AssistantView:
        context.require(Permission.ASSISTANTS_MANAGE)
        now = self._clock.now()
        async with self._uow() as uow:
            current = await uow.assistants.get_for_update(assistant_id)
            if current is None:
                raise NotFoundError(ASSISTANT_NOT_FOUND)
            assistant = _apply(current, changes)
            changed = _changed_fields(current, assistant)
            if changed:
                await _check_references(uow, context, assistant, current)
                if "name" in changed:
                    other = await uow.assistants.get_by_name(assistant.name)
                    if other is not None and other.id != assistant.id:
                        raise AssistantNameTakenError(NAME_TAKEN)
                try:
                    await uow.assistants.update(assistant, at=now)
                except UniqueViolationError:
                    raise AssistantNameTakenError(NAME_TAKEN) from None
                assistant.updated_at = now
                await uow.audit.record(
                    by_user(
                        context,
                        client,
                        AuditAction.ASSISTANT_UPDATED,
                        target_type="assistant",
                        target_id=assistant.id,
                        details={"fields": ",".join(changed)},  # names only, never the instructions
                    )
                )
                await uow.commit()
            state = (await availability(uow, context, [assistant]))[assistant.id]
        return AssistantView(assistant, state, True)


class DeleteAssistant:
    """Soft delete: the assistant disappears for everyone at once; its knowledge bases are untouched."""

    def __init__(self, uow: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def execute(self, context: SecurityContext, client: ClientInfo, assistant_id: UUID) -> None:
        context.require(Permission.ASSISTANTS_MANAGE)
        now = self._clock.now()
        async with self._uow() as uow:
            assistant = await uow.assistants.get_for_update(assistant_id)
            if assistant is None:
                raise NotFoundError(ASSISTANT_NOT_FOUND)
            await uow.assistants.mark_deleted(assistant.id, at=now)
            await uow.audit.record(
                by_user(
                    context,
                    client,
                    AuditAction.ASSISTANT_DELETED,
                    target_type="assistant",
                    target_id=assistant.id,
                )
            )
            await uow.commit()
