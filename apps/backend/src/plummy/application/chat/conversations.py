"""Conversations and their history (API_CONTRACTS.md §2.13).

A conversation is visible only to its owner: any other caller gets ``404`` as for one that does not exist.
Starting one needs ``chat.use`` and an assistant the caller can use (ADR-005). Reading the history of one's
own conversation does not depend on the assistant: it stays readable after access to it was lost, while
opening a citation checks access to the document again (SECURITY_MODEL.md §6.3).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from plummy.application.assistants.access import ASSISTANT_NOT_FOUND, availability, require_assistant
from plummy.application.audit import by_user
from plummy.application.errors import InvalidInputError, NotFoundError, PermissionDeniedError
from plummy.application.pagination import Page, PageRequest
from plummy.application.ports import Clock, UnitOfWork, UnitOfWorkFactory
from plummy.application.security import ClientInfo, SecurityContext
from plummy.core.ids import new_id
from plummy.domain.audit import AuditAction
from plummy.domain.chat import DEFAULT_TITLE, Conversation, InvalidChatInputError, Message, normalize_title
from plummy.domain.identity import Permission

CONVERSATION_NOT_FOUND: Final = "Conversation not found."
CHAT_DENIED: Final = "Chatting needs the chat.use permission."


@dataclass(frozen=True, slots=True)
class ConversationView:
    conversation: Conversation
    #: ``None`` only if the assistant row is gone; a deleted assistant keeps its name here.
    assistant_name: str | None
    #: Whether the caller can send a new message now (ADR-005, evaluated on this request).
    assistant_available: bool


def _title(value: str | None) -> str:
    if value is None:
        return DEFAULT_TITLE
    try:
        return normalize_title(value)
    except InvalidChatInputError as exc:
        raise InvalidInputError(exc.field, exc.code, str(exc)) from None


async def require_conversation(
    uow: UnitOfWork, context: SecurityContext, conversation_id: UUID
) -> Conversation:
    conversation = await uow.conversations.get_owned(conversation_id, context.user_id)
    if conversation is None:
        raise NotFoundError(CONVERSATION_NOT_FOUND)
    return conversation


async def _views(
    uow: UnitOfWork, context: SecurityContext, conversations: Sequence[Conversation]
) -> list[ConversationView]:
    ids = sorted({conversation.assistant_id for conversation in conversations})
    names = await uow.assistants.names(ids)
    live = await uow.assistants.get_many(ids)
    states = await availability(uow, context, live)
    can_chat = context.has_permission(Permission.CHAT_USE)
    return [
        ConversationView(
            conversation=conversation,
            assistant_name=names.get(conversation.assistant_id),
            assistant_available=can_chat
            and conversation.assistant_id in states
            and states[conversation.assistant_id].available,
        )
        for conversation in conversations
    ]


class ListConversations:
    def __init__(self, uow: UnitOfWorkFactory) -> None:
        self._uow = uow

    async def execute(
        self, context: SecurityContext, page: PageRequest, *, assistant_id: UUID | None = None
    ) -> Page[ConversationView]:
        if not context.has_permission(Permission.CHAT_USE):
            raise PermissionDeniedError(CHAT_DENIED)
        async with self._uow() as uow:
            result = await uow.conversations.page(context.user_id, page, assistant_id=assistant_id)
            items = await _views(uow, context, result.items)
        return Page(items=items, next_cursor=result.next_cursor)


class CreateConversation:
    """An empty conversation with an assistant the caller can use now; otherwise ``404`` (ADR-005)."""

    def __init__(self, uow: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def execute(
        self, context: SecurityContext, assistant_id: UUID, title: str | None = None
    ) -> ConversationView:
        context.require(Permission.CHAT_USE)
        clean_title = _title(title)
        now = self._clock.now()
        async with self._uow() as uow:
            assistant, state = await require_assistant(uow, context, assistant_id)
            if not state.available:
                raise NotFoundError(ASSISTANT_NOT_FOUND)  # a manager sees it, but nobody can chat with it
            conversation = Conversation(
                id=new_id(),
                user_id=context.user_id,
                assistant_id=assistant.id,
                title=clean_title,
                created_at=now,
                updated_at=now,
                last_message_at=now,
            )
            await uow.conversations.add(conversation)
            await uow.commit()
        return ConversationView(conversation, assistant.name, True)


class GetConversation:
    def __init__(self, uow: UnitOfWorkFactory) -> None:
        self._uow = uow

    async def execute(self, context: SecurityContext, conversation_id: UUID) -> ConversationView:
        async with self._uow() as uow:
            conversation = await require_conversation(uow, context, conversation_id)
            [view] = await _views(uow, context, [conversation])
        return view


class RenameConversation:
    def __init__(self, uow: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def execute(self, context: SecurityContext, conversation_id: UUID, title: str) -> ConversationView:
        clean_title = _title(title)
        now = self._clock.now()
        async with self._uow() as uow:
            conversation = await uow.conversations.get_owned_for_update(conversation_id, context.user_id)
            if conversation is None:
                raise NotFoundError(CONVERSATION_NOT_FOUND)
            if clean_title != conversation.title:
                await uow.conversations.rename(conversation.id, clean_title, at=now)
                conversation.title, conversation.updated_at = clean_title, now
            [view] = await _views(uow, context, [conversation])
            await uow.commit()
        return view


class DeleteConversation:
    """Hidden at once; the messages are removed by the chat maintenance sweep."""

    def __init__(self, uow: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def execute(self, context: SecurityContext, client: ClientInfo, conversation_id: UUID) -> None:
        now = self._clock.now()
        async with self._uow() as uow:
            conversation = await uow.conversations.get_owned_for_update(conversation_id, context.user_id)
            if conversation is None:
                raise NotFoundError(CONVERSATION_NOT_FOUND)
            await uow.conversations.mark_deleted(conversation.id, at=now)
            await uow.audit.record(
                by_user(
                    context,
                    client,
                    AuditAction.CONVERSATION_DELETED,
                    target_type="conversation",
                    target_id=conversation.id,
                )
            )
            await uow.commit()


class ListMessages:
    def __init__(self, uow: UnitOfWorkFactory) -> None:
        self._uow = uow

    async def execute(
        self, context: SecurityContext, conversation_id: UUID, page: PageRequest
    ) -> Page[Message]:
        async with self._uow() as uow:
            conversation = await require_conversation(uow, context, conversation_id)
            return await uow.messages.page(conversation.id, page)
