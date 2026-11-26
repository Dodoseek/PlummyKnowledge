"""The deletions file of ``maintenance export-deletions`` / ``reapply-deletions`` (ADR-023 §6)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from plummy.application.maintenance.deletions import (
    FORMAT,
    REAPPLIED,
    Deletion,
    DeletionExport,
    DeletionsFileError,
)

AT = datetime(2026, 11, 26, 3, 0, tzinfo=UTC)


def valid(**overrides: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "format": FORMAT,
        "since": AT.isoformat(),
        "exported_at": AT.isoformat(),
        "deletions": [
            {
                "action": "document.deleted",
                "target_type": "document",
                "target_id": str(uuid4()),
                "at": AT.isoformat(),
            }
        ],
        "not_reapplied": {"user.disabled": 2},
    }
    return data | overrides


def test_round_trip_for_every_reapplied_action() -> None:
    export = DeletionExport(
        since=AT,
        exported_at=AT,
        deletions=[Deletion(action, uuid4(), AT) for action in REAPPLIED],
        not_reapplied={"role.revoked": 1},
    )
    assert DeletionExport.from_json(export.to_json()) == export


def test_an_empty_export_is_valid() -> None:
    export = DeletionExport.from_json(json.dumps(valid(deletions=[], not_reapplied={})))
    assert export.deletions == []
    assert export.not_reapplied == {}


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("not json at all", "not JSON"),
        (json.dumps([1, 2]), FORMAT),
        (json.dumps(valid(format="plummy-deletions/2")), FORMAT),
        (json.dumps(valid(deletions="x")), "malformed"),
        (json.dumps(valid(since="yesterday")), "malformed"),
        (json.dumps(valid(since="2026-11-26T03:00:00")), "malformed"),  # no offset
        (json.dumps(valid(not_reapplied=[1])), "malformed"),
    ],
)
def test_invalid_files_are_refused(text: str, message: str) -> None:
    with pytest.raises(DeletionsFileError, match=message):
        DeletionExport.from_json(text)


@pytest.mark.parametrize(
    "item",
    [
        {
            "action": "assistant.deleted",
            "target_type": "assistant",
            "target_id": str(uuid4()),
            "at": AT.isoformat(),
        },
        {
            "action": "document.deleted",
            "target_type": "user",
            "target_id": str(uuid4()),
            "at": AT.isoformat(),
        },
        {
            "action": "document.deleted",
            "target_type": "document",
            "target_id": "../../etc",
            "at": AT.isoformat(),
        },
        {"action": "document.deleted", "target_type": "document", "at": AT.isoformat()},
        "document.deleted",
    ],
)
def test_only_known_deletions_of_the_right_type_are_accepted(item: Any) -> None:
    with pytest.raises(DeletionsFileError):
        DeletionExport.from_json(json.dumps(valid(deletions=[item])))


def test_the_error_never_echoes_the_file() -> None:
    with pytest.raises(DeletionsFileError) as error:
        DeletionExport.from_json(json.dumps(valid(deletions=[{"action": "sk-live-secret-value"}])))
    assert "sk-live" not in str(error.value)


def test_the_file_holds_identifiers_and_times_only() -> None:
    export = DeletionExport(AT, AT, [Deletion("user.deleted", uuid4(), AT)], {})
    item = json.loads(export.to_json())["deletions"][0]
    assert set(item) == {"action", "target_type", "target_id", "at"}
