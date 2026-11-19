"""Documents, versions, chunks and ingestion jobs (DATABASE_SCHEMA.md §5.7-5.10, §7.1, ADR-008).

Pure data and rules; no I/O. A version is immutable by content (checksum, storage key); only its processing
status and results change.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID

MAX_FILENAME_LENGTH: Final = 255


class DocumentStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    INDEXED = "indexed"
    FAILED = "failed"
    SKIPPED = "skipped"


class VersionStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"
    OUTDATED = "outdated"
    DELETED = "deleted"


class DeletionReason(StrEnum):
    USER = "user"
    REMOTE_DELETED = "remote_deleted"
    OUT_OF_SCOPE = "out_of_scope"
    SOURCE_DELETED = "source_deleted"
    KB_DELETED = "kb_deleted"


class JobKind(StrEnum):
    INGEST = "ingest"
    REINDEX = "reindex"
    DELETE = "delete"
    PURGE = "purge"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DEAD = "dead"

    @property
    def finished(self) -> bool:
        return self in (JobStatus.SUCCEEDED, JobStatus.FAILED, JobStatus.CANCELLED, JobStatus.DEAD)


class JobStage(StrEnum):
    """Pipeline stages of ADR-008 §3, in order."""

    FETCH = "fetch"
    AUTHORIZE = "authorize"
    VALIDATE_TYPE = "validate_type"
    SECURITY_CHECK = "security_check"
    STORE_ORIGINAL = "store_original"
    PARSE = "parse"
    NORMALIZE = "normalize"
    CHUNK = "chunk"
    EMBED = "embed"
    PERSIST = "persist"
    FINALIZE = "finalize"
    REPORT = "report"


class JobResult(StrEnum):
    PROCESSED = "processed"
    SKIPPED_ALREADY_PROCESSED = "skipped_already_processed"
    SKIPPED_UNCHANGED = "skipped_unchanged"


@dataclass(slots=True)
class Document:
    id: UUID
    data_source_id: UUID
    external_id: str
    filename: str  # for display only, never a storage path (§12)
    mime_type: str | None
    processing_status: DocumentStatus
    current_version_id: UUID | None
    created_at: datetime
    updated_at: datetime
    source_metadata: dict[str, str] = field(default_factory=dict)
    deleted_at: datetime | None = None
    deletion_reason: DeletionReason | None = None
    purged_at: datetime | None = None


@dataclass(slots=True)
class DocumentVersion:
    id: UUID
    document_id: UUID
    version_number: int
    checksum_sha256: bytes
    mime_type: str
    size_bytes: int
    storage_key: str
    status: VersionStatus
    created_at: datetime
    updated_at: datetime
    page_count: int | None = None
    extracted_storage_key: str | None = None
    parser: str | None = None
    error_code: str | None = None
    active_chunk_set: int | None = None
    processed_at: datetime | None = None


@dataclass(slots=True)
class IngestionJob:
    id: UUID
    document_id: UUID
    document_version_id: UUID | None
    kind: JobKind
    status: JobStatus
    stage: JobStage
    idempotency_key: str
    attempt: int
    max_attempts: int
    requested_by: UUID | None
    queued_at: datetime
    result: JobResult | None = None
    error_code: str | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


def display_filename(value: str | None) -> str:
    """A safe display name: the last path component, printable characters only, bounded length.

    The result is shown to users and written to ``Content-Disposition`` with RFC 6266 encoding; it is never
    used to build a storage path.
    """
    raw = (value or "").replace("\\", "/").rsplit("/", 1)[-1]
    cleaned = "".join(char for char in raw if char.isprintable()).strip().strip(".")
    if not cleaned:
        return "document"
    if len(cleaned) > MAX_FILENAME_LENGTH:
        stem, dot, extension = cleaned.rpartition(".")
        if dot and 0 < len(extension) <= 16:
            return stem[: MAX_FILENAME_LENGTH - len(extension) - 1] + "." + extension
        return cleaned[:MAX_FILENAME_LENGTH]
    return cleaned


def upload_external_id(document_id: UUID) -> str:
    """The server-generated source ID of an uploaded document (DATABASE_SCHEMA.md §5.7)."""
    return f"upload:{document_id.hex}"


def chunking_fingerprint(settings: Mapping[str, object]) -> str:
    """A short stable hash of the chunking settings, part of the job idempotency key (ADR-008 §4)."""
    canonical = json.dumps(settings, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def ingest_idempotency_key(
    version_id: UUID, embedding_profile_id: UUID, chunking: Mapping[str, object]
) -> str:
    """``(version, kind, chunking settings, embedding profile)`` (ADR-008 §4)."""
    return f"ingest:{version_id.hex}:{embedding_profile_id.hex}:{chunking_fingerprint(chunking)}"
