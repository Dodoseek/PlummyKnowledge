"""Conversations and messages on PostgreSQL (DATABASE_SCHEMA.md §6). Every conversation read is scoped to its
owner in the query itself."""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Select, delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from plummy.application.pagination import Page, PageRequest
from plummy.application.ports import UniqueViolationError
from plummy.domain.chat import (
    Conversation,
    GenerationRun,
    Message,
    MessageCitation,
    MessageRole,
    MessageStatus,
    RunFinishReason,
    RunStatus,
)
from plummy.infrastructure.db.models import (
    ConversationModel,
    GenerationRunModel,
    MessageCitationModel,
    MessageModel,
)
from plummy.infrastructure.db.paging import paginate

CLIENT_MESSAGE_UNIQUE = "uq_messages_conversation_id_client_message_id"


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

    async def touch(self, conversation_id: UUID, *, at: datetime, title: str | None = None) -> None:
        values: dict[str, Any] = {"last_message_at": at, "updated_at": at}
        if title is not None:
            values["title"] = title
        await self._db.execute(
            update(ConversationModel).where(ConversationModel.id == conversation_id).values(**values)
        )

    async def purge_deleted(self, *, limit: int) -> int:
        ids = (
            select(ConversationModel.id)
            .where(ConversationModel.deleted_at.is_not(None))
            .order_by(ConversationModel.deleted_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
            .scalar_subquery()
        )
        result = await self._db.execute(
            delete(ConversationModel).where(ConversationModel.id.in_(ids)).returning(ConversationModel.id)
        )
        return len(result.all())


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

    async def find_sent(self, user_id: UUID, client_message_id: UUID) -> tuple[Message, UUID | None] | None:
        row = await self._db.scalar(
            select(MessageModel)
            .join(ConversationModel, ConversationModel.id == MessageModel.conversation_id)
            .where(
                ConversationModel.user_id == user_id,
                ConversationModel.deleted_at.is_(None),
                MessageModel.client_message_id == client_message_id,
            )
            .limit(1)
        )
        if row is None:
            return None
        answer = await self._db.scalar(
            select(MessageModel.id).where(
                MessageModel.conversation_id == row.conversation_id,
                MessageModel.sequence == row.sequence + 1,
                MessageModel.role == MessageRole.ASSISTANT.value,
            )
        )
        return _message(row), answer

    async def next_sequence(self, conversation_id: UUID) -> int:
        last = await self._db.scalar(
            select(func.max(MessageModel.sequence)).where(MessageModel.conversation_id == conversation_id)
        )
        return (last or 0) + 1

    async def add(self, message: Message) -> None:
        self._db.add(
            MessageModel(
                id=message.id,
                conversation_id=message.conversation_id,
                sequence=message.sequence,
                role=message.role.value,
                content=message.content,
                status=message.status.value,
                client_message_id=message.client_message_id,
                error_code=message.error_code,
                created_at=message.created_at,
                completed_at=message.completed_at,
            )
        )
        try:
            await self._db.flush()
        except IntegrityError as exc:
            if CLIENT_MESSAGE_UNIQUE in str(exc.orig):
                raise UniqueViolationError(CLIENT_MESSAGE_UNIQUE) from None
            raise

    async def recent(self, conversation_id: UUID, *, before_sequence: int, limit: int) -> list[Message]:
        rows = await self._db.scalars(
            select(MessageModel)
            .where(MessageModel.conversation_id == conversation_id, MessageModel.sequence < before_sequence)
            .order_by(MessageModel.sequence.desc())
            .limit(limit)
        )
        return [_message(row) for row in reversed(rows.all())]

    async def finish(
        self,
        message_id: UUID,
        *,
        content: str,
        status: MessageStatus,
        error_code: str | None,
        at: datetime,
        citations: Sequence[MessageCitation],
    ) -> None:
        await self._db.execute(
            update(MessageModel)
            .where(MessageModel.id == message_id)
            .values(content=content, status=status.value, error_code=error_code, completed_at=at)
        )
        for citation in citations:
            self._db.add(
                MessageCitationModel(
                    message_id=message_id,
                    citation_key=citation.citation_key,
                    ordinal=citation.ordinal,
                    document_id=citation.document_id,
                    document_version_id=citation.document_version_id,
                    chunk_id=citation.chunk_id,
                    filename=citation.filename,
                    page=citation.page,
                    section=citation.section,
                    sheet=citation.sheet,
                    row_index=citation.row,
                    snippet=citation.snippet,
                    viewer_route=citation.viewer_route,
                )
            )
        await self._db.flush()

    async def fail_streaming(self, *, started_before: datetime, error_code: str, at: datetime) -> list[UUID]:
        result = await self._db.execute(
            update(MessageModel)
            .where(
                MessageModel.status == MessageStatus.STREAMING.value,
                MessageModel.created_at < started_before,
            )
            .values(status=MessageStatus.FAILED.value, error_code=error_code, completed_at=at)
            .returning(MessageModel.id)
        )
        return [row[0] for row in result.all()]


def _run(row: GenerationRunModel) -> GenerationRun:
    return GenerationRun(
        id=row.id,
        conversation_id=row.conversation_id,
        message_id=row.message_id,
        assistant_id=row.assistant_id,
        model_config_id=row.model_config_id,
        embedding_config_id=row.embedding_config_id,
        status=RunStatus(row.status),
        started_at=row.started_at,
        finish_reason=None if row.finish_reason is None else RunFinishReason(row.finish_reason),
        retrieved_chunk_ids=tuple(row.retrieved_chunk_ids),
        rejected_citation_count=row.rejected_citation_count,
        prompt_tokens=row.prompt_tokens,
        completion_tokens=row.completion_tokens,
        retrieval_ms=row.retrieval_ms,
        time_to_first_token_ms=row.time_to_first_token_ms,
        total_ms=row.total_ms,
        error_code=row.error_code,
        finished_at=row.finished_at,
    )


class SqlGenerationRunRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._db = session

    async def add(self, run: GenerationRun) -> None:
        self._db.add(
            GenerationRunModel(
                id=run.id,
                conversation_id=run.conversation_id,
                message_id=run.message_id,
                assistant_id=run.assistant_id,
                model_config_id=run.model_config_id,
                embedding_config_id=run.embedding_config_id,
                status=run.status.value,
                started_at=run.started_at,
            )
        )
        await self._db.flush()

    async def get_owned(self, run_id: UUID, user_id: UUID) -> GenerationRun | None:
        row = await self._db.scalar(
            select(GenerationRunModel)
            .join(ConversationModel, ConversationModel.id == GenerationRunModel.conversation_id)
            .where(
                GenerationRunModel.id == run_id,
                ConversationModel.user_id == user_id,
                ConversationModel.deleted_at.is_(None),
            )
        )
        return None if row is None else _run(row)

    async def finish(self, run: GenerationRun) -> None:
        await self._db.execute(
            update(GenerationRunModel)
            .where(GenerationRunModel.id == run.id)
            .values(
                status=run.status.value,
                finish_reason=None if run.finish_reason is None else run.finish_reason.value,
                retrieved_chunk_ids=list(run.retrieved_chunk_ids),
                rejected_citation_count=run.rejected_citation_count,
                prompt_tokens=run.prompt_tokens,
                completion_tokens=run.completion_tokens,
                retrieval_ms=run.retrieval_ms,
                time_to_first_token_ms=run.time_to_first_token_ms,
                total_ms=run.total_ms,
                error_code=run.error_code,
                finished_at=run.finished_at,
            )
        )

    async def fail_for_messages(self, message_ids: Sequence[UUID], *, error_code: str, at: datetime) -> int:
        if not message_ids:
            return 0
        result = await self._db.execute(
            update(GenerationRunModel)
            .where(
                GenerationRunModel.message_id.in_(message_ids),
                GenerationRunModel.status == RunStatus.RUNNING.value,
            )
            .values(
                status=RunStatus.FAILED.value,
                finish_reason=RunFinishReason.ERROR.value,
                error_code=error_code,
                finished_at=at,
            )
            .returning(GenerationRunModel.id)
        )
        return len(result.all())
