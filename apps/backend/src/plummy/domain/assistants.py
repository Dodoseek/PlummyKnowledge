"""Assistants (DATABASE_SCHEMA.md §5.3, ADR-005).

An assistant answers from its knowledge bases with its instructions, chat model and retrieval settings.
Who may use it is not stored here: it follows from the access to every one of its knowledge bases
(ADR-005).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID

MAX_ASSISTANT_NAME_LENGTH: Final = 200
MAX_ASSISTANT_DESCRIPTION_LENGTH: Final = 2000
MAX_INSTRUCTIONS_LENGTH: Final = 20_000
MAX_HISTORY_MESSAGES: Final = 100
MAX_OUTPUT_TOKENS: Final = 128_000
DEFAULT_HISTORY_MESSAGES: Final = 10
DEFAULT_OUTPUT_TOKENS: Final = 1024


class AssistantStatus(StrEnum):
    ACTIVE = "active"
    DISABLED = "disabled"  # nobody can chat with it (ADR-005 §5)


class Unavailability(StrEnum):
    """Why the current user cannot use an assistant (API_CONTRACTS.md §2.12)."""

    DISABLED = "disabled"
    NO_KNOWLEDGE_BASES = "no_knowledge_bases"
    MISSING_KB_ACCESS = "missing_kb_access"


@dataclass(frozen=True, slots=True)
class AssistantRetrieval:
    """Retrieval settings of the assistant; validated by the retrieval settings of the application."""

    top_k: int
    vector_top_k: int
    keyword_top_k: int
    rrf_k: int
    vector_weight: float
    keyword_weight: float


@dataclass(slots=True)
class Assistant:
    id: UUID
    name: str
    description: str | None
    instructions: str
    model_config_id: UUID
    embedding_profile_id: UUID
    retrieval: AssistantRetrieval
    max_context_tokens: int
    max_history_messages: int
    max_output_tokens: int
    status: AssistantStatus
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime
    knowledge_base_ids: tuple[UUID, ...] = field(default=())
    deleted_at: datetime | None = None


class InvalidAssistantInputError(ValueError):
    def __init__(self, field: str, code: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.code = code


def normalize_instructions(value: str) -> str:
    """Kept as written except surrounding whitespace; control characters other than newline and tab are
    refused. Instructions must not carry secrets (SECURITY_MODEL.md §8): they are sent to the model."""
    text = value.strip()
    if len(text) > MAX_INSTRUCTIONS_LENGTH or any(not (c.isprintable() or c in "\n\t") for c in text):
        raise InvalidAssistantInputError(
            "instructions",
            "invalid_instructions",
            f"The instructions must be at most {MAX_INSTRUCTIONS_LENGTH} printable characters.",
        )
    return text
