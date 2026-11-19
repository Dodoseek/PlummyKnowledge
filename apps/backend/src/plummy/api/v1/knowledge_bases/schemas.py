"""DTOs for ``/api/v1/knowledge-bases`` (API_CONTRACTS.md §2.10, §4)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from plummy.application.knowledge.use_cases import KnowledgeBaseView
from plummy.application.pagination import Page
from plummy.domain.knowledge import MAX_KB_DESCRIPTION_LENGTH, MAX_KB_NAME_LENGTH, KbAccessLevel


class IndexingResponse(BaseModel):
    documents_total: int
    indexed: int
    processing: int
    failed: int


class KnowledgeBaseResponse(BaseModel):
    id: UUID
    name: str
    description: str | None
    embedding_profile_id: UUID
    indexing: IndexingResponse
    my_access_level: KbAccessLevel
    created_at: datetime
    updated_at: datetime

    @classmethod
    def build(cls, view: KnowledgeBaseView) -> KnowledgeBaseResponse:
        kb, stats = view.knowledge_base, view.indexing
        return cls(
            id=kb.id,
            name=kb.name,
            description=kb.description,
            embedding_profile_id=kb.embedding_profile_id,
            indexing=IndexingResponse(
                documents_total=stats.documents_total,
                indexed=stats.indexed,
                processing=stats.processing,
                failed=stats.failed,
            ),
            my_access_level=view.access_level,
            created_at=kb.created_at,
            updated_at=kb.updated_at,
        )


class KnowledgeBaseListResponse(BaseModel):
    items: list[KnowledgeBaseResponse]
    next_cursor: str | None
    has_more: bool

    @classmethod
    def from_page(cls, page: Page[KnowledgeBaseView]) -> KnowledgeBaseListResponse:
        return cls(
            items=[KnowledgeBaseResponse.build(item) for item in page.items],
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )


class CreateKnowledgeBaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=MAX_KB_NAME_LENGTH * 2)  # normalized and checked by the domain
    description: str | None = Field(default=None, max_length=MAX_KB_DESCRIPTION_LENGTH * 2)
    embedding_profile_id: UUID
