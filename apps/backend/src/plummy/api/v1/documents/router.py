"""``/api/v1/documents`` (API_CONTRACTS.md §2.11). The use cases check knowledge base access."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status

from plummy.api.dependencies import ResourcesDep
from plummy.api.security import CurrentContext, SettingsDep, client_info
from plummy.api.v1.common import allow_query, no_store, page_request
from plummy.api.v1.documents.multipart import MultipartUpload
from plummy.api.v1.documents.schemas import (
    DocumentListResponse,
    DocumentResponse,
    ReindexResponse,
    SourceViewResponse,
    UploadResponse,
    VersionListResponse,
    VersionResponse,
)
from plummy.api.v1.jobs.schemas import JobResponse
from plummy.application.errors import InvalidInputError
from plummy.application.pagination import SortSpec
from plummy.domain.documents import DocumentStatus

router = APIRouter(prefix="/documents", tags=["documents"], dependencies=[Depends(no_store)])

UPLOAD_FIELDS = frozenset({"knowledge_base_id"})
MAX_SEARCH_LENGTH = 200
SORTS = (
    SortSpec("-created_at", "created_at", True, datetime),
    SortSpec("created_at", "created_at", False, datetime),
    SortSpec("filename", "filename", False, str),
    SortSpec("-filename", "filename", True, str),
)


@router.get(
    "",
    response_model=DocumentListResponse,
    dependencies=[
        Depends(
            allow_query(
                "knowledge_base_id", "processing_status", "data_source_id", "q", "sort", "limit", "cursor"
            )
        )
    ],
)
async def list_documents(
    knowledge_base_id: UUID,
    context: CurrentContext,
    resources: ResourcesDep,
    processing_status: DocumentStatus | None = None,
    data_source_id: UUID | None = None,
    q: Annotated[str | None, Query(max_length=MAX_SEARCH_LENGTH)] = None,
    sort: str | None = None,
    limit: int | None = None,
    cursor: str | None = None,
) -> DocumentListResponse:
    """``knowledge_base_id`` is required: documents are always listed through a knowledge base."""
    page = page_request(sort, limit, cursor, SORTS, SORTS[0])
    result = await resources.documents.list.execute(
        context,
        knowledge_base_id,
        page,
        status=processing_status,
        data_source_id=data_source_id,
        search=(q.strip() or None) if q else None,
    )
    return DocumentListResponse.from_page(result)


@router.post(
    "",
    response_model=UploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["knowledge_base_id", "file"],
                        "properties": {
                            "knowledge_base_id": {"type": "string", "format": "uuid"},
                            "file": {"type": "string", "format": "binary"},
                        },
                    }
                }
            },
        }
    },
)
async def upload_document(
    request: Request, context: CurrentContext, resources: ResourcesDep, settings: SettingsDep
) -> UploadResponse:
    """Fields must precede the file: access is checked before the file is read."""
    upload = MultipartUpload(request, max_file_bytes=settings.upload_max_bytes)
    fields, filename = await upload.fields_before_file()
    unknown = set(fields) - UPLOAD_FIELDS
    if unknown:
        raise InvalidInputError(sorted(unknown)[0], "unknown_field", "Unknown form field.")
    try:
        kb_id = UUID(fields["knowledge_base_id"])
    except (KeyError, ValueError):
        raise InvalidInputError(
            "knowledge_base_id", "invalid_uuid", "knowledge_base_id must be a UUID sent before the file."
        ) from None
    result = await resources.documents.upload.execute(
        context, client_info(request), kb_id, filename, upload.file_content()
    )
    return UploadResponse.build(result)


@router.get("/{document_id}", response_model=DocumentResponse)
async def get_document(
    document_id: UUID, context: CurrentContext, resources: ResourcesDep
) -> DocumentResponse:
    return DocumentResponse.build(await resources.documents.get.execute(context, document_id))


@router.get("/{document_id}/versions", response_model=VersionListResponse)
async def list_document_versions(
    document_id: UUID, context: CurrentContext, resources: ResourcesDep
) -> VersionListResponse:
    versions = await resources.documents.versions.execute(context, document_id)
    return VersionListResponse(items=[VersionResponse.build(version) for version in versions])


@router.get("/{document_id}/versions/{version_id}/chunks/{chunk_id}/view", response_model=SourceViewResponse)
async def view_source(
    document_id: UUID, version_id: UUID, chunk_id: UUID, context: CurrentContext, resources: ResourcesDep
) -> SourceViewResponse:
    """The cited passage with its neighbours; access is checked again on every request."""
    view = await resources.documents.view.execute(context, document_id, version_id, chunk_id)
    return SourceViewResponse.build(view)


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT, response_class=Response)
async def delete_document(
    document_id: UUID, request: Request, context: CurrentContext, resources: ResourcesDep
) -> Response:
    """The document leaves search at once; chunks, vectors and files are purged in the background."""
    await resources.documents.delete.execute(context, client_info(request), document_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{document_id}/reindex", response_model=ReindexResponse, status_code=status.HTTP_202_ACCEPTED)
async def reindex_document(
    document_id: UUID, request: Request, context: CurrentContext, resources: ResourcesDep
) -> ReindexResponse:
    result = await resources.documents.reindex.execute(context, client_info(request), document_id)
    return ReindexResponse(job=JobResponse.build(result.job), created=result.created)
