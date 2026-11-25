"""DTOs for ``/api/v1/assistants`` (API_CONTRACTS.md §2.12, §4)."""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from plummy.application.assistants.use_cases import AssistantChanges, AssistantView, RetrievalChanges
from plummy.application.pagination import Page
from plummy.application.retrieval.model import MAX_KNOWLEDGE_BASES
from plummy.domain.assistants import (
    MAX_ASSISTANT_DESCRIPTION_LENGTH,
    MAX_ASSISTANT_NAME_LENGTH,
    MAX_INSTRUCTIONS_LENGTH,
    AssistantStatus,
    Unavailability,
)


class RetrievalBody(BaseModel):
    """Ranges are checked by the use case (the same rules as retrieval itself)."""

    model_config = ConfigDict(extra="forbid")

    top_k: int | None = None
    vector_top_k: int | None = None
    keyword_top_k: int | None = None
    rrf_k: int | None = None
    vector_weight: float | None = Field(default=None, allow_inf_nan=False)
    keyword_weight: float | None = Field(default=None, allow_inf_nan=False)


class LimitsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_context_tokens: int | None = None
    max_history_messages: int | None = None
    max_output_tokens: int | None = None


class AssistantBody(BaseModel):
    """``POST`` and ``PATCH``: only the fields sent are applied; ``description: null`` clears it."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=MAX_ASSISTANT_NAME_LENGTH * 2)
    description: str | None = Field(default=None, max_length=MAX_ASSISTANT_DESCRIPTION_LENGTH * 2)
    instructions: str | None = Field(default=None, max_length=MAX_INSTRUCTIONS_LENGTH * 2)
    model_config_id: UUID | None = None
    embedding_profile_id: UUID | None = None
    knowledge_base_ids: list[UUID] | None = Field(default=None, max_length=MAX_KNOWLEDGE_BASES * 2)
    retrieval: RetrievalBody | None = None
    limits: LimitsBody | None = None
    status: AssistantStatus | None = None

    def changes(self) -> AssistantChanges:
        sent = set(self.model_fields_set) - {"limits"}
        limits = self.limits or LimitsBody()
        sent |= {name for name in limits.model_fields_set if getattr(limits, name) is not None}
        retrieval = self.retrieval or RetrievalBody()
        if "retrieval" in sent and self.retrieval is None:
            sent.discard("retrieval")
        for name in (
            "name",
            "instructions",
            "model_config_id",
            "embedding_profile_id",
            "knowledge_base_ids",
            "status",
        ):
            if name in sent and getattr(self, name) is None:
                sent.discard(name)  # ``null`` for a required value means "unchanged"
        return AssistantChanges(
            sent=frozenset(sent),
            name=self.name,
            description=self.description,
            instructions=self.instructions,
            model_config_id=self.model_config_id,
            embedding_profile_id=self.embedding_profile_id,
            knowledge_base_ids=tuple(self.knowledge_base_ids)
            if self.knowledge_base_ids is not None
            else None,
            retrieval=RetrievalChanges(**retrieval.model_dump()),
            max_context_tokens=limits.max_context_tokens,
            max_history_messages=limits.max_history_messages,
            max_output_tokens=limits.max_output_tokens,
            status=self.status,
        )


class CreateAssistantBody(AssistantBody):
    name: str = Field(min_length=1, max_length=MAX_ASSISTANT_NAME_LENGTH * 2)
    model_config_id: UUID
    embedding_profile_id: UUID


class RetrievalResponse(BaseModel):
    top_k: int
    vector_top_k: int
    keyword_top_k: int
    rrf_k: int
    vector_weight: float
    keyword_weight: float


class LimitsResponse(BaseModel):
    max_context_tokens: int
    max_history_messages: int
    max_output_tokens: int


class AvailabilityResponse(BaseModel):
    available: bool
    reason: Unavailability | None


class AssistantResponse(BaseModel):
    id: UUID
    name: str
    description: str | None
    instructions: str | None  # only for assistants.manage
    model_config_id: UUID
    embedding_profile_id: UUID
    knowledge_base_ids: list[UUID]
    retrieval: RetrievalResponse
    limits: LimitsResponse
    status: AssistantStatus
    availability: AvailabilityResponse
    created_at: datetime
    updated_at: datetime

    @classmethod
    def build(cls, view: AssistantView) -> AssistantResponse:
        a = view.assistant
        retrieval: dict[str, Any] = {
            name: getattr(a.retrieval, name) for name in RetrievalResponse.model_fields
        }
        return cls(
            id=a.id,
            name=a.name,
            description=a.description,
            instructions=a.instructions if view.show_instructions else None,
            model_config_id=a.model_config_id,
            embedding_profile_id=a.embedding_profile_id,
            knowledge_base_ids=list(a.knowledge_base_ids),
            retrieval=RetrievalResponse(**retrieval),
            limits=LimitsResponse(
                max_context_tokens=a.max_context_tokens,
                max_history_messages=a.max_history_messages,
                max_output_tokens=a.max_output_tokens,
            ),
            status=a.status,
            availability=AvailabilityResponse(
                available=view.availability.available, reason=view.availability.reason
            ),
            created_at=a.created_at,
            updated_at=a.updated_at,
        )


class AssistantListResponse(BaseModel):
    items: list[AssistantResponse]
    next_cursor: str | None
    has_more: bool

    @classmethod
    def from_page(cls, page: Page[AssistantView]) -> AssistantListResponse:
        return cls(
            items=[AssistantResponse.build(view) for view in page.items],
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )
