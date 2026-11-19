"""Knowledge base access policy (SECURITY_MODEL.md §3.4-3.5, ADR-012).

Effective access = the strongest of a direct grant, grants of the user's non-deleted groups and
``knowledge_bases.manage`` (``manage`` on every knowledge base). It is computed from the verified session on
every request; identifiers from the client only name the knowledge base being asked for.
"""

from __future__ import annotations

from typing import Final
from uuid import UUID

from plummy.application.errors import NotFoundError, PermissionDeniedError
from plummy.application.ports import UnitOfWork
from plummy.application.security import SecurityContext
from plummy.domain.identity import Permission
from plummy.domain.knowledge import KbAccessLevel, KnowledgeBase, strongest

KB_NOT_FOUND: Final = "Knowledge base not found."
KB_MANAGE_DENIED: Final = "You do not have permission to manage this knowledge base."


async def effective_access(
    uow: UnitOfWork, context: SecurityContext, kb_ids: list[UUID]
) -> dict[UUID, KbAccessLevel]:
    """Access level per knowledge base; knowledge bases without access are absent."""
    if context.has_permission(Permission.KNOWLEDGE_BASES_MANAGE):
        return dict.fromkeys(kb_ids, KbAccessLevel.MANAGE)
    grants = await uow.knowledge_bases.grants_of_user(context.user_id, kb_ids)
    levels: dict[UUID, KbAccessLevel] = {}
    for kb_id, granted in grants.items():
        level = strongest(granted)
        if level is not None:
            levels[kb_id] = level
    return levels


async def require_kb_access(
    uow: UnitOfWork, context: SecurityContext, kb_id: UUID, required: KbAccessLevel
) -> tuple[KnowledgeBase, KbAccessLevel]:
    """The knowledge base and the caller's level.

    Without any access the answer is ``404`` as for a missing knowledge base (its existence is not disclosed);
    with ``read`` where ``manage`` is required it is ``403`` (API_CONTRACTS.md §1.4).
    """
    kb = await uow.knowledge_bases.get(kb_id)
    level = (await effective_access(uow, context, [kb_id])).get(kb_id) if kb is not None else None
    if kb is None or level is None:
        raise NotFoundError(KB_NOT_FOUND)
    if not level.allows(required):
        raise PermissionDeniedError(KB_MANAGE_DENIED)
    return kb, level
