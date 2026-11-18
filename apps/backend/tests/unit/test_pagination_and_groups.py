"""Cursor pagination (ADR-014) and group input rules, without a database."""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from uuid import UUID

import pytest

from plummy.application.errors import InvalidQueryParameterError
from plummy.application.pagination import (
    DEFAULT_LIMIT,
    MAX_LIMIT,
    SortSpec,
    decode_cursor,
    encode_cursor,
    escape_like,
    parse_limit,
    parse_sort,
)
from plummy.domain.identity import (
    InvalidGroupDescriptionError,
    InvalidGroupNameError,
    normalize_group_description,
    normalize_group_name,
)

BY_EMAIL = SortSpec("email", "email", False, str)
BY_EMAIL_DESC = SortSpec("-email", "email", True, str)
BY_CREATED = SortSpec("created_at", "created_at", False, datetime)
ROW_ID = UUID("0190f3c4-0000-7000-8000-000000000001")


def _raw(payload: object) -> str:
    return base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=")


@pytest.mark.parametrize("value", ["alice@example.com", "ünïcødé", ""])
def test_string_cursor_round_trip(value: str) -> None:
    cursor = decode_cursor(encode_cursor(BY_EMAIL, value, ROW_ID), BY_EMAIL)
    assert cursor is not None
    assert (cursor.value, cursor.id) == (value, ROW_ID)


def test_datetime_cursor_round_trip() -> None:
    moment = datetime(2026, 11, 18, 12, 30, 15, 123456, tzinfo=UTC)
    cursor = decode_cursor(encode_cursor(BY_CREATED, moment, ROW_ID), BY_CREATED)
    assert cursor is not None
    assert cursor.value == moment


def test_absent_cursor_is_first_page() -> None:
    assert decode_cursor(None, BY_EMAIL) is None


@pytest.mark.parametrize(
    ("raw", "sort"),
    [
        ("", BY_EMAIL),
        ("!!!", BY_EMAIL),
        ("a" * 2000, BY_EMAIL),
        (_raw([1, 2]), BY_EMAIL),
        (_raw({"v": 2, "s": "email", "k": ["a", str(ROW_ID)]}), BY_EMAIL),
        (_raw({"v": 1, "s": "-email", "k": ["a", str(ROW_ID)]}), BY_EMAIL),
        (_raw({"v": 1, "s": "email", "k": ["a"]}), BY_EMAIL),
        (_raw({"v": 1, "s": "email", "k": [1, str(ROW_ID)]}), BY_EMAIL),
        (_raw({"v": 1, "s": "email", "k": ["a", "not-a-uuid"]}), BY_EMAIL),
        (_raw({"v": 1, "s": "created_at", "k": ["2026-11-18T12:00:00", str(ROW_ID)]}), BY_CREATED),
        (_raw({"v": 1, "s": "created_at", "k": ["yesterday", str(ROW_ID)]}), BY_CREATED),
    ],
)
def test_malformed_cursors_are_rejected(raw: str, sort: SortSpec) -> None:
    with pytest.raises(InvalidQueryParameterError) as exc:
        decode_cursor(raw, sort)
    assert exc.value.field == "cursor"


def test_cursor_from_another_sort_is_rejected() -> None:
    cursor = encode_cursor(BY_EMAIL, "a@example.com", ROW_ID)
    with pytest.raises(InvalidQueryParameterError):
        decode_cursor(cursor, BY_EMAIL_DESC)


def test_parse_sort_uses_allowlist() -> None:
    allowed = [BY_EMAIL, BY_EMAIL_DESC]
    assert parse_sort(None, allowed, default=BY_EMAIL) is BY_EMAIL
    assert parse_sort("-email", allowed, default=BY_EMAIL) is BY_EMAIL_DESC
    with pytest.raises(InvalidQueryParameterError) as exc:
        parse_sort("password_hash", allowed, default=BY_EMAIL)
    assert exc.value.field == "sort"


@pytest.mark.parametrize(("value", "expected"), [(None, DEFAULT_LIMIT), (1, 1), (MAX_LIMIT, MAX_LIMIT)])
def test_parse_limit_accepts_range(value: int | None, expected: int) -> None:
    assert parse_limit(value) == expected


@pytest.mark.parametrize("value", [0, -1, MAX_LIMIT + 1])
def test_parse_limit_rejects_out_of_range(value: int) -> None:
    with pytest.raises(InvalidQueryParameterError):
        parse_limit(value)


def test_escape_like_escapes_wildcards_and_backslash() -> None:
    assert escape_like(r"50%_off\now") == r"50\%\_off\\now"


def test_group_name_is_trimmed_and_validated() -> None:
    assert normalize_group_name("  Sales  ") == "Sales"
    assert normalize_group_name("x" * 100) == "x" * 100
    for bad in ("", "   ", "x" * 101, "tab\tname", "zero​width"):
        with pytest.raises(InvalidGroupNameError):
            normalize_group_name(bad)


def test_group_description_rules() -> None:
    assert normalize_group_description(None) is None
    assert normalize_group_description("   ") is None
    assert normalize_group_description(" line one\nline two\t ") == "line one\nline two"
    for bad in ("d" * 501, "bell\u0007"):
        with pytest.raises(InvalidGroupDescriptionError):
            normalize_group_description(bad)
