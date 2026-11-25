"""External document sources: credentials and sync jobs (DATABASE_SCHEMA.md §5.5, §7.2, ADR-020).

Pure data and rules; no I/O. Secrets never appear here: credentials keep only the reference to the encrypted
refresh token.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final
from uuid import UUID

from plummy.domain.documents import JobStatus

DEFAULT_SYNC_INTERVAL_MINUTES: Final = 15
MIN_SYNC_INTERVAL_MINUTES: Final = 5
MAX_SYNC_INTERVAL_MINUTES: Final = 24 * 60
RECONCILIATION_INTERVAL: Final = timedelta(days=1)
MAX_SYNC_ATTEMPTS: Final = 5
MAX_ACCOUNT_LABEL_LENGTH: Final = 320


class CredentialStatus(StrEnum):
    VALID = "valid"
    EXPIRED = "expired"
    REVOKED = "revoked"


@dataclass(slots=True)
class SourceCredentials:
    data_source_id: UUID
    secret_ref_id: UUID
    account_label: str | None
    granted_scopes: tuple[str, ...]
    status: CredentialStatus
    created_at: datetime
    updated_at: datetime
    connected_by: UUID | None = None
    last_refreshed_at: datetime | None = None
    revoked_at: datetime | None = None


class SyncTrigger(StrEnum):
    MANUAL = "manual"
    SCHEDULE = "schedule"
    RECONCILIATION = "reconciliation"
    INITIAL = "initial"


class SyncMode(StrEnum):
    INITIAL = "initial"  # full listing of the folder, then the cursor taken before it is saved
    INCREMENTAL = "incremental"  # changes since the saved cursor
    RECONCILIATION = "reconciliation"  # full listing compared with the index


@dataclass(slots=True)
class SyncStats:
    added: int = 0
    updated: int = 0
    renamed: int = 0
    deleted: int = 0
    skipped: int = 0
    failed: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "added": self.added,
            "updated": self.updated,
            "renamed": self.renamed,
            "deleted": self.deleted,
            "skipped": self.skipped,
            "failed": self.failed,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, object]) -> SyncStats:
        def count(key: str) -> int:
            value = raw.get(key, 0)
            return value if isinstance(value, int) and value >= 0 else 0

        return cls(
            added=count("added"),
            updated=count("updated"),
            renamed=count("renamed"),
            deleted=count("deleted"),
            skipped=count("skipped"),
            failed=count("failed"),
        )


@dataclass(slots=True)
class SyncJob:
    id: UUID
    data_source_id: UUID
    trigger: SyncTrigger
    mode: SyncMode
    status: JobStatus
    queued_at: datetime
    updated_at: datetime
    requested_by: UUID | None = None
    cursor_before: str | None = None
    cursor_after: str | None = None
    stats: SyncStats = field(default_factory=SyncStats)
    attempt: int = 0
    max_attempts: int = MAX_SYNC_ATTEMPTS
    error_code: str | None = None
    error_message: str | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class InvalidSyncIntervalError(ValueError):
    pass


def check_sync_interval(minutes: int | None) -> int | None:
    """``None`` means manual sync only; otherwise 5 minutes to 24 hours."""
    if minutes is None:
        return None
    if isinstance(minutes, bool) or not MIN_SYNC_INTERVAL_MINUTES <= minutes <= MAX_SYNC_INTERVAL_MINUTES:
        raise InvalidSyncIntervalError(
            f"The sync interval must be {MIN_SYNC_INTERVAL_MINUTES}-{MAX_SYNC_INTERVAL_MINUTES} minutes."
        )
    return minutes


def next_sync_after(finished_at: datetime, interval_minutes: int | None) -> datetime | None:
    """When the scheduler starts the next incremental sync; ``None`` for manual-only sources."""
    if interval_minutes is None:
        return None
    return finished_at + timedelta(minutes=interval_minutes)


def reconciliation_due(last_reconciled_at: datetime | None, now: datetime) -> bool:
    return last_reconciled_at is None or now - last_reconciled_at >= RECONCILIATION_INTERVAL


def normalize_account_label(value: str | None) -> str | None:
    """A display label from the provider; control characters dropped and length bounded."""
    if value is None:
        return None
    label = "".join(char for char in value if char.isprintable()).strip()
    return label[:MAX_ACCOUNT_LABEL_LENGTH] or None
