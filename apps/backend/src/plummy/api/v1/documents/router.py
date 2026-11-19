"""``/api/v1/documents`` (API_CONTRACTS.md §2.11). The use cases check knowledge base access."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, Request, status

from plummy.api.dependencies import ResourcesDep
from plummy.api.security import CurrentContext, SettingsDep, client_info
from plummy.api.v1.common import no_store
from plummy.api.v1.documents.multipart import MultipartUpload
from plummy.api.v1.documents.schemas import UploadResponse
from plummy.application.errors import InvalidInputError

router = APIRouter(prefix="/documents", tags=["documents"], dependencies=[Depends(no_store)])

UPLOAD_FIELDS = frozenset({"knowledge_base_id"})


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
