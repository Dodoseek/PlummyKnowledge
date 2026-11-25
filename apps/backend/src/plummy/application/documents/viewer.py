"""The protected in-app source viewer (API_CONTRACTS.md §2.11, SECURITY_MODEL.md §6.3).

Opening a citation is a new request for document text: access to a knowledge base of the document is checked
again every time; a citation in the history grants nothing. A deleted document, a chunk of another version
or document, and a missing grant all look the same (``404``), so the viewer reveals neither existence nor
content.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final
from uuid import UUID

from plummy.application.documents.use_cases import DOCUMENT_NOT_FOUND, require_document_access
from plummy.application.errors import NotFoundError
from plummy.application.ports import ChunkNeighbourhood, UnitOfWorkFactory
from plummy.application.security import SecurityContext
from plummy.domain.documents import Document, DocumentVersion
from plummy.domain.knowledge import KbAccessLevel

CONTEXT_CHUNKS: Final = 2  # neighbours on each side of the cited chunk


@dataclass(frozen=True, slots=True)
class SourceView:
    document: Document
    version: DocumentVersion
    is_current: bool
    passage: ChunkNeighbourhood


class ViewSource:
    def __init__(self, uow: UnitOfWorkFactory) -> None:
        self._uow = uow

    async def execute(
        self, context: SecurityContext, document_id: UUID, version_id: UUID, chunk_id: UUID
    ) -> SourceView:
        async with self._uow() as uow:
            document, _ = await require_document_access(
                uow, context, await uow.documents.get(document_id), KbAccessLevel.READ
            )
            version = await uow.documents.get_version(version_id)
            if version is None or version.document_id != document.id:
                raise NotFoundError(DOCUMENT_NOT_FOUND)
            passage = await uow.chunks.around(document.id, version.id, chunk_id, neighbours=CONTEXT_CHUNKS)
            if passage is None:
                raise NotFoundError(DOCUMENT_NOT_FOUND)
        return SourceView(document, version, document.current_version_id == version.id, passage)
