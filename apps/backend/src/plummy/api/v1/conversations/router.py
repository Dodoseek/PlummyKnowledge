"""``/api/v1/conversations`` (API_CONTRACTS.md §2.13). A conversation is private to its owner; the use cases
scope every read to the verified session's user."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response, status

from plummy.api.dependencies import ResourcesDep
from plummy.api.security import CurrentContext, client_info
from plummy.api.v1.common import allow_query, no_store, page_request
from plummy.api.v1.conversations.schemas import (
    ConversationListResponse,
    ConversationResponse,
    CreateConversationRequest,
    MessageListResponse,
    RenameConversationRequest,
)
from plummy.application.pagination import SortSpec

router = APIRouter(prefix="/conversations", tags=["conversations"], dependencies=[Depends(no_store)])

SORTS = (
    SortSpec("-last_message_at", "last_message_at", True, datetime),
    SortSpec("-created_at", "created_at", True, datetime),
)
# Newest first by default: the chat shows the latest messages and loads older ones on demand.
MESSAGE_SORTS = (
    SortSpec("-sequence", "sequence", True, int),
    SortSpec("sequence", "sequence", False, int),
)


@router.get(
    "",
    response_model=ConversationListResponse,
    dependencies=[Depends(allow_query("sort", "limit", "cursor", "assistant_id"))],
)
async def list_conversations(
    context: CurrentContext,
    resources: ResourcesDep,
    sort: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
    assistant_id: UUID | None = None,
) -> ConversationListResponse:
    page = page_request(sort, limit, cursor, SORTS, SORTS[0])
    result = await resources.conversations.list.execute(context, page, assistant_id=assistant_id)
    return ConversationListResponse.from_page(result)


@router.post("", response_model=ConversationResponse, status_code=status.HTTP_201_CREATED)
async def create_conversation(
    body: CreateConversationRequest, context: CurrentContext, resources: ResourcesDep
) -> ConversationResponse:
    view = await resources.conversations.create.execute(context, body.assistant_id, body.title)
    return ConversationResponse.build(view)


@router.get("/{conversation_id}", response_model=ConversationResponse)
async def get_conversation(
    conversation_id: UUID, context: CurrentContext, resources: ResourcesDep
) -> ConversationResponse:
    return ConversationResponse.build(await resources.conversations.get.execute(context, conversation_id))


@router.patch("/{conversation_id}", response_model=ConversationResponse)
async def rename_conversation(
    conversation_id: UUID, body: RenameConversationRequest, context: CurrentContext, resources: ResourcesDep
) -> ConversationResponse:
    view = await resources.conversations.rename.execute(context, conversation_id, body.title)
    return ConversationResponse.build(view)


@router.delete("/{conversation_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_conversation(
    conversation_id: UUID, request: Request, context: CurrentContext, resources: ResourcesDep
) -> Response:
    await resources.conversations.delete.execute(context, client_info(request), conversation_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/{conversation_id}/messages",
    response_model=MessageListResponse,
    dependencies=[Depends(allow_query("sort", "limit", "cursor"))],
)
async def list_messages(
    conversation_id: UUID,
    context: CurrentContext,
    resources: ResourcesDep,
    sort: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> MessageListResponse:
    page = page_request(sort, limit, cursor, MESSAGE_SORTS, MESSAGE_SORTS[0])
    return MessageListResponse.from_page(
        await resources.conversations.messages.execute(context, conversation_id, page)
    )
