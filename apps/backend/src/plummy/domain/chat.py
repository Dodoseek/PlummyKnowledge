"""Conversations and messages (DATABASE_SCHEMA.md §6, ADR-019).

A conversation is private to the user who started it. Who may continue it follows from the access to its
assistant on every message (ADR-005); the history itself stays readable by its owner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID

MAX_TITLE_LENGTH: Final = 200
MAX_QUESTION_LENGTH: Final = 4000
TITLE_FROM_QUESTION_LENGTH: Final = 80
DEFAULT_TITLE: Final = "New conversation"


class MessageRole(StrEnum):
    USER = "user"
    ASSISTANT = "assistant"


class MessageStatus(StrEnum):
    COMPLETED = "completed"
    STREAMING = "streaming"
    STOPPED = "stopped"
    FAILED = "failed"


@dataclass(slots=True)
class Conversation:
    id: UUID
    user_id: UUID
    assistant_id: UUID
    title: str
    created_at: datetime
    updated_at: datetime
    last_message_at: datetime
    deleted_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class MessageCitation:
    """A validated citation as it was when the answer was written. Opening it checks access again."""

    citation_key: str
    ordinal: int
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


@dataclass(slots=True)
class Message:
    id: UUID
    conversation_id: UUID
    sequence: int
    role: MessageRole
    content: str
    status: MessageStatus
    client_message_id: UUID | None
    created_at: datetime
    completed_at: datetime | None = None
    error_code: str | None = None
    citations: tuple[MessageCitation, ...] = field(default=())


class InvalidChatInputError(ValueError):
    def __init__(self, field: str, code: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.code = code


def _printable(text: str, *, newlines: bool) -> bool:
    allowed = "\n\t" if newlines else ""
    return all(c.isprintable() or c in allowed for c in text)


def normalize_title(value: str) -> str:
    """Whitespace collapsed; 1-200 printable characters."""
    title = " ".join(value.split())
    if not 1 <= len(title) <= MAX_TITLE_LENGTH or not _printable(title, newlines=False):
        raise InvalidChatInputError(
            "title", "invalid_title", f"The title must be 1-{MAX_TITLE_LENGTH} printable characters."
        )
    return title


def normalize_question(value: str) -> str:
    """Kept as written except surrounding whitespace; newlines and tabs allowed, other controls refused."""
    question = value.strip()
    if not 1 <= len(question) <= MAX_QUESTION_LENGTH or not _printable(question, newlines=True):
        raise InvalidChatInputError(
            "content",
            "invalid_content",
            f"The message must be 1-{MAX_QUESTION_LENGTH} printable characters.",
        )
    return question


def title_from_question(question: str) -> str:
    """The start of the first question, on one line."""
    text = " ".join(question.split())
    if len(text) <= TITLE_FROM_QUESTION_LENGTH:
        return text or DEFAULT_TITLE
    return text[: TITLE_FROM_QUESTION_LENGTH - 1].rstrip() + "…"
