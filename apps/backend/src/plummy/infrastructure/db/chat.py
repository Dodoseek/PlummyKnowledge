"""Conversations and messages on PostgreSQL (DATABASE_SCHEMA.md §6). Every conversation read is scoped to its
owner in the query itself."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from sqlalchemy import Select, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from plummy.application.pagination import Page, PageRequest
from plummy.domain.chat import Conversation, Message, MessageCitation, MessageRole, MessageStatus
from plummy.infrastructure.db.models import ConversationModel, MessageCitationModel, MessageModel
from plummy.infrastructure.db.paging import paginate


def _conversation(row: ConversationModel) -> Conversation:
    return Conversation(
        id=row.id,
        user_id=row.user_id,
        assistant_id=row.assistant_id,
        title=row.title,
        created_at=row.created_at,
        updated_at=row.updated_at,
        last_message_at=row.last_message_at,
        deleted_at=row.deleted_at,
    )


def _citation(row: MessageCitationModel) -> MessageCitation:
    return MessageCitation(
        citation_key=row.citation_key,
        ordinal=row.ordinal,
        document_id=row.document_id,
        document_version_id=row.document_version_id,
        chunk_id=row.chunk_id,
        filename=row.filename,
        page=row.page,
        section=row.section,
        sheet=row.sheet,
        row=row.row_index,
        snippet=row.snippet,
        viewer_route=row.viewer_route,
    )


def _message(row: MessageModel, citations: Sequence[MessageCitation] = ()) -> Message:
    return Message(
        id=row.id,
        conversation_id=row.conversation_id,
        sequence=row.sequence,
        role=MessageRole(row.role),
        content=row.content,
        status=MessageStatus(row.status),
        client_message_id=row.client_message_id,
        created_at=row.created_at,
        completed_at=row.completed_at,
        error_code=row.error_code,
        citations=tuple(citations),
    )


def _owned(conversation_id: UUID, user_id: UUID) -> Select[ConversationModel]:
    return select(ConversationModel).where(
        ConversationModel.id == conversation_id,
        ConversationModel.user_id == user_id,
        ConversationModel.deleted_at.is_(None),
    )


class SqlConversationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session

    async def add(self, conversation: Conversation) -> None:
        self._db.add(
            ConversationModel(
                id=conversation.id,
                user_id=conversation.user_id,
                assistant_id=conversation.assistant_id,
                title=conversation.title,
                created_at=conversation.created_at,
                updated_at=conversation.updated_at,
                last_message_at=conversation.last_message_at,
            )
        )
        await self._db.flush()

    async def get_owned(self, conversation_id: UUID, user_id: UUID) -> Conversation | None:
        row = await self._db.scalar(_owned(conversation_id, user_id))
        return None if row is None else _conversation(row)

    async def get_owned_for_update(self, conversation_id: UUID, user_id: UUID) -> Conversation | None:
        row = await self._db.scalar(
            _owned(conversation_id, user_id).with_for_update().execution_options(populate_existing=True)
        )
        return None if row is None else _conversation(row)

    async def page(
        self, user_id: UUID, page: PageRequest, *, assistant_id: UUID | None = None
    ) -> Page[Conversation]:
        statement = select(ConversationModel).where(
            ConversationModel.user_id == user_id, ConversationModel.deleted_at.is_(None)
        )
        if assistant_id is not None:
            statement = statement.where(ConversationModel.assistant_id == assistant_id)
        return await paginate(self._db, statement, ConversationModel, page, _conversation)

    async def rename(self, conversation_id: UUID, title: str, *, at: datetime) -> None:
        await self._db.execute(
            update(ConversationModel)
            .where(ConversationModel.id == conversation_id)
            .values(title=title, updated_at=at)
        )

    async def mark_deleted(self, conversation_id: UUID, *, at: datetime) -> None:
        await self._db.execute(
            update(ConversationModel)
            .where(ConversationModel.id == conversation_id, ConversationModel.deleted_at.is_(None))
            .values(deleted_at=at, updated_at=at)
        )


class SqlMessageRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session

    async def _citations(self, message_ids: Sequence[UUID]) -> dict[UUID, list[MessageCitation]]:
        if not message_ids:
            return {}
        rows = await self._db.scalars(
            select(MessageCitationModel)
            .where(MessageCitationModel.message_id.in_(message_ids))
            .order_by(MessageCitationModel.message_id, MessageCitationModel.ordinal)
        )
        result: dict[UUID, list[MessageCitation]] = {}
        for row in rows.all():
            result.setdefault(row.message_id, []).append(_citation(row))
        return result

    async def page(self, conversation_id: UUID, page: PageRequest) -> Page[Message]:
        statement = select(MessageModel).where(MessageModel.conversation_id == conversation_id)
        result = await paginate(self._db, statement, MessageModel, page, lambda row: row)
        citations = await self._citations([row.id for row in result.items])
        return Page(
            items=[_message(row, citations.get(row.id, [])) for row in result.items],
            next_cursor=result.next_cursor,
        )
