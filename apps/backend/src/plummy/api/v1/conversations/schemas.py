"""DTOs for ``/api/v1/conversations`` (API_CONTRACTS.md §2.13, §3.3)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from plummy.application.chat.conversations import ConversationView
from plummy.application.pagination import Page
from plummy.domain.chat import MAX_TITLE_LENGTH, Message, MessageCitation, MessageRole, MessageStatus


class CreateConversationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    assistant_id: UUID
    # The use case collapses whitespace and enforces the real 1-200 limit.
    title: str | None = Field(default=None, max_length=MAX_TITLE_LENGTH * 2)


class RenameConversationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=MAX_TITLE_LENGTH * 2)


class ConversationAssistant(BaseModel):
    id: UUID
    name: str | None
    #: Whether a new message can be sent now (ADR-005, evaluated on this request).
    available: bool


class ConversationResponse(BaseModel):
    id: UUID
    title: str
    assistant: ConversationAssistant
    created_at: datetime
    updated_at: datetime
    last_message_at: datetime

    @classmethod
    def build(cls, view: ConversationView) -> ConversationResponse:
        c = view.conversation
        return cls(
            id=c.id,
            title=c.title,
            assistant=ConversationAssistant(
                id=c.assistant_id, name=view.assistant_name, available=view.assistant_available
            ),
            created_at=c.created_at,
            updated_at=c.updated_at,
            last_message_at=c.last_message_at,
        )


class ConversationListResponse(BaseModel):
    items: list[ConversationResponse]
    next_cursor: str | None
    has_more: bool

    @classmethod
    def from_page(cls, page: Page[ConversationView]) -> ConversationListResponse:
        return cls(
            items=[ConversationResponse.build(view) for view in page.items],
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )


class CitationResponse(BaseModel):
    """The ``citation`` SSE event shape (API_CONTRACTS.md §3.3), from the snapshot of the answer."""

    citation_id: str
    document_id: UUID
    document_version_id: UUID
    chunk_id: UUID
    filename: str
    page: int | None
    section: str | None
    sheet: str | None
    row: int | None
    snippet: str
    viewer_route: str

    @classmethod
    def build(cls, citation: MessageCitation) -> CitationResponse:
        return cls(
            citation_id=citation.citation_key,
            document_id=citation.document_id,
            document_version_id=citation.document_version_id,
            chunk_id=citation.chunk_id,
            filename=citation.filename,
            page=citation.page,
            section=citation.section,
            sheet=citation.sheet,
            row=citation.row,
            snippet=citation.snippet,
            viewer_route=citation.viewer_route,
        )


class MessageResponse(BaseModel):
    id: UUID
    sequence: int
    role: MessageRole
    content: str
    status: MessageStatus
    error_code: str | None
    citations: list[CitationResponse]
    created_at: datetime
    completed_at: datetime | None

    @classmethod
    def build(cls, message: Message) -> MessageResponse:
        return cls(
            id=message.id,
            sequence=message.sequence,
            role=message.role,
            content=message.content,
            status=message.status,
            error_code=message.error_code,
            citations=[CitationResponse.build(citation) for citation in message.citations],
            created_at=message.created_at,
            completed_at=message.completed_at,
        )


class MessageListResponse(BaseModel):
    items: list[MessageResponse]
    next_cursor: str | None
    has_more: bool

    @classmethod
    def from_page(cls, page: Page[Message]) -> MessageListResponse:
        return cls(
            items=[MessageResponse.build(message) for message in page.items],
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )
