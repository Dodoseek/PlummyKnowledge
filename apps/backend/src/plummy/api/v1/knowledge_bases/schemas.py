"""DTOs for ``/api/v1/knowledge-bases`` (API_CONTRACTS.md §2.10, §4)."""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from plummy.application.documents.use_cases import KnowledgeBaseReindexResult
from plummy.application.knowledge.use_cases import GrantView, KnowledgeBaseView
from plummy.application.pagination import Page
from plummy.application.retrieval.model import (
    DEFAULT_TOP_K,
    MAX_QUERY_LENGTH,
    MAX_TOP_K,
    RankedChunk,
    RetrievalResult,
    SearchMode,
)
from plummy.domain.knowledge import (
    MAX_KB_DESCRIPTION_LENGTH,
    MAX_KB_GRANTS,
    MAX_KB_NAME_LENGTH,
    KbAccessLevel,
    PrincipalType,
)


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


class UpdateKnowledgeBaseRequest(BaseModel):
    """Only the fields sent are changed; ``description: null`` clears the description."""

    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=MAX_KB_NAME_LENGTH * 2)
    description: str | None = Field(default=None, max_length=MAX_KB_DESCRIPTION_LENGTH * 2)
    embedding_profile_id: UUID | None = None


class GrantRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    principal_type: PrincipalType
    principal_id: UUID
    access_level: KbAccessLevel


class ReplaceAccessRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grants: list[GrantRequest] = Field(max_length=MAX_KB_GRANTS)


class GrantResponse(BaseModel):
    principal_type: PrincipalType
    principal_id: UUID
    name: str
    email: str | None
    access_level: KbAccessLevel
    granted_by: UUID | None
    granted_at: datetime | None

    @classmethod
    def build(cls, view: GrantView) -> GrantResponse:
        grant = view.grant
        return cls(
            principal_type=grant.principal_type,
            principal_id=grant.principal_id,
            name=view.name,
            email=view.email,
            access_level=grant.access_level,
            granted_by=grant.granted_by,
            granted_at=grant.created_at,
        )


class AccessResponse(BaseModel):
    items: list[GrantResponse]

    @classmethod
    def build(cls, views: list[GrantView]) -> AccessResponse:
        return cls(items=[GrantResponse.build(view) for view in views])


class KnowledgeBaseReindexResponse(BaseModel):
    """Documents with a queued or running job keep it; documents without a version are skipped."""

    jobs_created: int
    job_ids: list[UUID]
    already_queued: int
    without_version: int

    @classmethod
    def build(cls, result: KnowledgeBaseReindexResult) -> KnowledgeBaseReindexResponse:
        return cls(
            jobs_created=len(result.jobs),
            job_ids=[job.id for job in result.jobs],
            already_queued=result.already_queued,
            without_version=result.without_version,
        )


class SearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # Whitespace is collapsed by the use case, which enforces the real 1-1000 character limit.
    query: str = Field(min_length=1, max_length=MAX_QUERY_LENGTH * 4)
    mode: SearchMode = SearchMode.HYBRID
    top_k: int = Field(default=DEFAULT_TOP_K, ge=1, le=MAX_TOP_K)


class SearchHitResponse(BaseModel):
    chunk_id: UUID
    document_id: UUID
    document_version_id: UUID
    filename: str
    chunk_index: int
    content: str
    token_count: int
    page_start: int | None
    page_end: int | None
    section_path: list[str]
    sheet_name: str | None
    row_start: int | None
    row_end: int | None
    score: float
    vector_rank: int | None
    keyword_rank: int | None

    @classmethod
    def build(cls, item: RankedChunk) -> SearchHitResponse:
        hit = item.hit
        return cls(
            chunk_id=hit.chunk_id,
            document_id=hit.document_id,
            document_version_id=hit.version_id,
            filename=hit.filename,
            chunk_index=hit.chunk_index,
            content=hit.content,
            token_count=hit.token_count,
            page_start=hit.page_start,
            page_end=hit.page_end,
            section_path=list(hit.section_path),
            sheet_name=hit.sheet_name,
            row_start=hit.row_start,
            row_end=hit.row_end,
            score=round(item.fused_score, 6),
            vector_rank=item.vector_rank,
            keyword_rank=item.keyword_rank,
        )


class SearchResponse(BaseModel):
    mode: SearchMode
    items: list[SearchHitResponse]
    total_tokens: int
    keyword_query_empty: bool

    @classmethod
    def build(cls, result: RetrievalResult) -> SearchResponse:
        return cls(
            mode=result.mode,
            items=[SearchHitResponse.build(item) for item in result.chunks],
            total_tokens=result.total_tokens,
            keyword_query_empty=result.keyword_query_empty,
        )
