"""Rules of external sources (ADR-020 §8): sync interval, schedule, reconciliation, labels and stats."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from plummy.application.sources.jobs import first_mode
from plummy.domain.knowledge import DataSource, DataSourceStatus, DataSourceType
from plummy.domain.sources import (
    InvalidSyncIntervalError,
    SyncMode,
    SyncStats,
    check_sync_interval,
    next_sync_after,
    normalize_account_label,
    reconciliation_due,
)
from plummy.workers.tasks.connectors import MAX_RETRY_DELAY_SECONDS, RETRY_BACKOFF_SECONDS, retry_delay

NOW = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)


@pytest.mark.parametrize("minutes", [None, 5, 15, 1440])
def test_valid_sync_intervals(minutes: int | None) -> None:
    assert check_sync_interval(minutes) == minutes


@pytest.mark.parametrize("minutes", [0, 4, 1441, -15, True])
def test_invalid_sync_intervals(minutes: int) -> None:
    with pytest.raises(InvalidSyncIntervalError):
        check_sync_interval(minutes)


def test_next_sync_follows_the_interval_and_manual_sources_are_not_scheduled() -> None:
    assert next_sync_after(NOW, 15) == NOW + timedelta(minutes=15)
    assert next_sync_after(NOW, None) is None


def test_reconciliation_is_due_once_a_day() -> None:
    assert reconciliation_due(None, NOW)
    assert not reconciliation_due(NOW - timedelta(hours=23), NOW)
    assert reconciliation_due(NOW - timedelta(days=1), NOW)


@pytest.mark.parametrize(
    ("raw", "label"),
    [
        (None, None),
        ("  ", None),
        ("Drive Bot <drive@example.com>", "Drive Bot <drive@example.com>"),
        ("evil‮\nlabel\x00", "evillabel"),
        ("x" * 400, "x" * 320),
    ],
)
def test_account_labels_are_printable_and_bounded(raw: str | None, label: str | None) -> None:
    assert normalize_account_label(raw) == label


def test_stats_round_trip_and_ignore_garbage() -> None:
    stats = SyncStats(added=2, deleted=1)
    assert SyncStats.from_dict(dict(stats.as_dict())) == stats
    garbage: dict[str, object] = {"added": -1, "updated": "3", "skipped": 4, "other": 9}
    assert SyncStats.from_dict(garbage) == SyncStats(skipped=4)


def test_upload_source_is_not_external() -> None:
    def source(kind: DataSourceType) -> DataSource:
        return DataSource(
            id=uuid4(),
            type=kind,
            name="s",
            status=DataSourceStatus.ACTIVE,
            owner_knowledge_base_id=None,
            created_by=None,
            created_at=NOW,
            updated_at=NOW,
        )

    assert not source(DataSourceType.UPLOAD).is_external
    assert source(DataSourceType.GOOGLE_DRIVE).is_external


def test_sync_retry_delay_follows_the_backoff_and_retry_after() -> None:
    assert [retry_delay(n, None) for n in range(7)] == [*RETRY_BACKOFF_SECONDS, 3600, 3600]
    assert retry_delay(0, 42.5) == 42  # the provider asks for longer than the first backoff step
    assert retry_delay(2, 5.0) == RETRY_BACKOFF_SECONDS[2]  # never shorter than the backoff
    assert retry_delay(0, 10**9) == MAX_RETRY_DELAY_SECONDS


def test_first_sync_mode_depends_on_the_cursor() -> None:
    now = datetime.now(UTC)
    source = DataSource(
        id=uuid4(),
        type=DataSourceType.GOOGLE_DRIVE,
        name="Drive",
        status=DataSourceStatus.ACTIVE,
        owner_knowledge_base_id=None,
        created_by=None,
        created_at=now,
        updated_at=now,
    )
    assert first_mode(source) is SyncMode.INITIAL
    source.sync_cursor = "42"
    assert first_mode(source) is SyncMode.INCREMENTAL
