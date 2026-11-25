"""DTOs and SSE frames for ``/api/v1/chat`` (API_CONTRACTS.md §3)."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from plummy.api.v1.conversations.schemas import CitationResponse
from plummy.application.chat.send import (
    ChatEvent,
    CitationIssued,
    GenerationFailed,
    MessageCompleted,
    MessageStarted,
    TokenDelta,
)
from plummy.domain.chat import MAX_QUESTION_LENGTH


class ChatStreamRequest(BaseModel):
    """No knowledge base IDs: the backend takes them from the assistant (ADR-005)."""

    model_config = ConfigDict(extra="forbid")

    conversation_id: UUID | None = None
    assistant_id: UUID | None = None  # required when ``conversation_id`` is null
    # The use case trims and enforces the real 1-4000 limit.
    content: str = Field(min_length=1, max_length=MAX_QUESTION_LENGTH * 2)
    client_message_id: UUID


def frame(event: str, data: dict[str, Any]) -> bytes:
    """One SSE frame without ``id:`` (no ``Last-Event-ID`` in the MVP). JSON keeps the data on one line."""
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n".encode()


def _timestamp(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def event_frame(event: ChatEvent, *, correlation_id: str | None) -> bytes:
    match event:
        case MessageStarted():
            return frame(
                "message_start",
                {
                    "conversation_id": str(event.conversation_id),
                    "user_message_id": str(event.user_message_id),
                    "assistant_message_id": str(event.assistant_message_id),
                    "generation_run_id": str(event.generation_run_id),
                    "created_at": _timestamp(event.created_at),
                },
            )
        case TokenDelta():
            return frame("token", {"delta": event.delta})
        case CitationIssued():
            return frame("citation", CitationResponse.build(event.citation).model_dump(mode="json"))
        case MessageCompleted():
            return frame(
                "message_complete",
                {
                    "assistant_message_id": str(event.assistant_message_id),
                    "finish_reason": event.finish_reason.value,
                    "citation_ids": list(event.citation_ids),
                    "usage": {
                        "prompt_tokens": event.prompt_tokens,
                        "completion_tokens": event.completion_tokens,
                    },
                },
            )
        case GenerationFailed():
            return frame(
                "error",
                {
                    "code": event.code,
                    "message": event.message,
                    "retryable": event.retryable,
                    "correlation_id": correlation_id,
                },
            )


def heartbeat_frame(at: datetime) -> bytes:
    return frame("heartbeat", {"ts": _timestamp(at)})
