"""Titles, questions and the integer cursor of the message list (DATABASE_SCHEMA.md §6, ADR-014)."""

from __future__ import annotations

import base64
import json
from uuid import UUID

import pytest

from plummy.application.errors import InvalidQueryParameterError
from plummy.application.pagination import SortSpec, decode_cursor, encode_cursor
from plummy.domain.chat import (
    MAX_QUESTION_LENGTH,
    InvalidChatInputError,
    normalize_question,
    normalize_title,
    title_from_question,
)

BY_SEQUENCE = SortSpec("-sequence", "sequence", True, int)
ROW_ID = UUID("0190a000-0000-7000-8000-000000000001")


def test_titles_are_collapsed_and_bounded() -> None:
    assert normalize_title("  Leave \n  policy ") == "Leave policy"
    for bad in ("", "   ", "a\u0007b", "x" * 201):
        with pytest.raises(InvalidChatInputError):
            normalize_title(bad)


def test_questions_keep_their_lines() -> None:
    assert normalize_question("  Line one\n\tLine two  ") == "Line one\n\tLine two"
    for bad in ("", " \n ", "a\u0000b", "x" * (MAX_QUESTION_LENGTH + 1)):
        with pytest.raises(InvalidChatInputError):
            normalize_question(bad)


def test_title_from_question() -> None:
    assert title_from_question("How many\nvacation days?") == "How many vacation days?"
    long = title_from_question("word " * 40)
    assert len(long) == 80
    assert long.endswith("…")


def test_integer_cursor_round_trip() -> None:
    cursor = decode_cursor(encode_cursor(BY_SEQUENCE, 42, ROW_ID), BY_SEQUENCE)
    assert cursor is not None
    assert (cursor.value, cursor.id) == (42, ROW_ID)


@pytest.mark.parametrize("raw", ["4.2", "1e3", "abc", "", "٤٢", "9" * 13])
def test_a_forged_integer_cursor_is_refused(raw: str) -> None:
    payload = json.dumps({"v": 1, "s": BY_SEQUENCE.key, "k": [raw, str(ROW_ID)]}).encode()
    forged = base64.urlsafe_b64encode(payload).rstrip(b"=").decode()
    with pytest.raises(InvalidQueryParameterError):
        decode_cursor(forged, BY_SEQUENCE)
