"""Knowledge bases, their access and document sources (DATABASE_SCHEMA.md §5.1-5.6, ADR-008, ADR-012).

Pure data and rules; no I/O. Access is granted per knowledge base (no document-level ACL in the MVP).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID

MAX_KB_NAME_LENGTH: Final = 200
MAX_KB_DESCRIPTION_LENGTH: Final = 2000


class KbAccessLevel(StrEnum):
    READ = "read"  # search, chat, documents and the source viewer
    MANAGE = "manage"  # read + documents, sources, reindex and access of this knowledge base

    @property
    def rank(self) -> int:
        return 1 if self is KbAccessLevel.READ else 2

    def allows(self, required: KbAccessLevel) -> bool:
        return self.rank >= required.rank


def strongest(levels: list[KbAccessLevel]) -> KbAccessLevel | None:
    """Effective access: the maximum of direct and group grants (SECURITY_MODEL.md §3.5)."""
    return max(levels, key=lambda level: level.rank, default=None)


class PrincipalType(StrEnum):
    USER = "user"
    GROUP = "group"


class DataSourceType(StrEnum):
    UPLOAD = "upload"  # created with and owned by exactly one knowledge base (ADR-008 §1)
    GOOGLE_DRIVE = "google_drive"
    MICROSOFT_365 = "microsoft_365"


class DataSourceStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    ERROR = "error"
    DISCONNECTED = "disconnected"


class InvalidKnowledgeInputError(ValueError):
    def __init__(self, field: str, code: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.code = code


@dataclass(slots=True)
class KnowledgeBase:
    id: UUID
    name: str
    description: str | None
    embedding_profile_id: UUID
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None


MAX_KB_GRANTS: Final = 500


@dataclass(frozen=True, slots=True)
class KbGrant:
    """One entry of a knowledge base's access list (DATABASE_SCHEMA.md §5.2)."""

    principal_type: PrincipalType
    principal_id: UUID
    access_level: KbAccessLevel
    granted_by: UUID | None = None
    created_at: datetime | None = None

    @property
    def key(self) -> tuple[PrincipalType, UUID]:
        return (self.principal_type, self.principal_id)


class SyncOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(slots=True)
class DataSource:
    id: UUID
    type: DataSourceType
    name: str
    status: DataSourceStatus
    owner_knowledge_base_id: UUID | None
    created_by: UUID | None
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None
    # External sources only (ADR-020): the selected folder and the sync state. ``config`` never holds secrets.
    config: dict[str, str] = field(default_factory=dict)
    sync_cursor: str | None = None
    sync_interval_minutes: int | None = None
    next_sync_at: datetime | None = None
    last_sync_at: datetime | None = None
    last_reconciled_at: datetime | None = None
    last_sync_status: SyncOutcome | None = None
    last_error: str | None = None  # a typed error code, never provider output

    @property
    def is_external(self) -> bool:
        return self.type is not DataSourceType.UPLOAD


def normalize_kb_name(value: str) -> str:
    name = " ".join(value.split())
    if not name or len(name) > MAX_KB_NAME_LENGTH or not name.isprintable():
        raise InvalidKnowledgeInputError(
            "name", "invalid_name", f"The name must be 1-{MAX_KB_NAME_LENGTH} printable characters."
        )
    return name


def normalize_description(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    if len(text) > MAX_KB_DESCRIPTION_LENGTH or any(
        not (char.isprintable() or char in "\n\t") for char in text
    ):
        raise InvalidKnowledgeInputError(
            "description",
            "invalid_description",
            f"The description must be at most {MAX_KB_DESCRIPTION_LENGTH} printable characters.",
        )
    return text


def upload_source_name(kb_name: str) -> str:
    return f"Uploads: {kb_name}"[:MAX_KB_NAME_LENGTH]
