"""Who may use an assistant (ADR-005, SECURITY_MODEL.md §6.3).

An assistant is usable by a user only with ``read`` or better on **every** one of its knowledge bases
(direct, through a group, or ``knowledge_bases.manage``). An assistant without knowledge bases is for
``assistants.manage`` only (configuration and preview); a disabled one is for nobody. The rule is evaluated
from the verified session on every request; there is no stored access to an assistant.
``SqlAssistantRepository.page`` applies the same rule in SQL for the chat list, and the access matrix tests
keep the two in step.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from plummy.application.errors import NotFoundError
from plummy.application.knowledge.access import effective_access
from plummy.application.ports import UnitOfWork
from plummy.application.security import SecurityContext
from plummy.domain.assistants import Assistant, AssistantStatus, Unavailability
from plummy.domain.identity import Permission

ASSISTANT_NOT_FOUND: Final = "Assistant not found."


@dataclass(frozen=True, slots=True)
class Availability:
    """For the current user: ``reason`` is ``None`` exactly when the assistant is available."""

    reason: Unavailability | None

    @property
    def available(self) -> bool:
        return self.reason is None


def availability_of(
    context: SecurityContext, assistant: Assistant, readable: set[UUID] | frozenset[UUID]
) -> Availability:
    """``readable``: the knowledge bases among the assistant's ones the user has at least ``read`` on."""
    if assistant.status is AssistantStatus.DISABLED:
        return Availability(Unavailability.DISABLED)
    if not assistant.knowledge_base_ids:
        if context.has_permission(Permission.ASSISTANTS_MANAGE):
            return Availability(None)  # preview while it is being set up (ADR-005 §4)
        return Availability(Unavailability.NO_KNOWLEDGE_BASES)
    if not set(assistant.knowledge_base_ids) <= readable:
        return Availability(Unavailability.MISSING_KB_ACCESS)
    return Availability(None)


async def availability(
    uow: UnitOfWork, context: SecurityContext, assistants: Sequence[Assistant]
) -> dict[UUID, Availability]:
    """One grants lookup for all the knowledge bases of ``assistants``."""
    kb_ids = sorted({kb_id for assistant in assistants for kb_id in assistant.knowledge_base_ids})
    readable = set(await effective_access(uow, context, kb_ids)) if kb_ids else set()
    return {assistant.id: availability_of(context, assistant, readable) for assistant in assistants}


async def require_assistant(
    uow: UnitOfWork, context: SecurityContext, assistant_id: UUID
) -> tuple[Assistant, Availability]:
    """The assistant as the caller may see it. ``assistants.manage`` sees every assistant with its
    availability; anyone else only an available one with ``chat.use``, else ``404`` as for one that does
    not exist."""
    assistant = await uow.assistants.get(assistant_id)
    if assistant is None:
        raise NotFoundError(ASSISTANT_NOT_FOUND)
    state = (await availability(uow, context, [assistant]))[assistant.id]
    if context.has_permission(Permission.ASSISTANTS_MANAGE):
        return assistant, state
    if not context.has_permission(Permission.CHAT_USE) or not state.available:
        raise NotFoundError(ASSISTANT_NOT_FOUND)
    return assistant, state
