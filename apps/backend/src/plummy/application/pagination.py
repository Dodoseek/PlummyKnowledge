"""Cursor (keyset) pagination (ADR-014).

The cursor is ``base64url(JSON)`` of ``{"v": 1, "s": <sort>, "k": [<sort value>, <id>]}``. It only marks a
position: access filters are always applied by the server, so a forged cursor cannot reveal anything.
"""

from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from plummy.application.errors import InvalidQueryParameterError

DEFAULT_LIMIT = 25
MAX_LIMIT = 100
MAX_CURSOR_LENGTH = 1024
CURSOR_VERSION = 1

CursorValue = str | datetime | int


@dataclass(frozen=True, slots=True)
class SortSpec:
    """An allowed sort: ``key`` as in ``?sort=``; ``descending`` for a leading ``-``."""

    key: str
    field: str
    descending: bool
    value_type: type[str] | type[datetime] | type[int]


@dataclass(frozen=True, slots=True)
class Cursor:
    value: CursorValue
    id: UUID


@dataclass(frozen=True, slots=True)
class PageRequest:
    sort: SortSpec
    limit: int = DEFAULT_LIMIT
    after: Cursor | None = None


@dataclass(frozen=True, slots=True)
class Page[T]:
    items: list[T]
    next_cursor: str | None

    @property
    def has_more(self) -> bool:
        return self.next_cursor is not None


def parse_sort(value: str | None, allowed: Sequence[SortSpec], *, default: SortSpec) -> SortSpec:
    if value is None:
        return default
    for spec in allowed:
        if spec.key == value:
            return spec
    raise InvalidQueryParameterError(
        "sort", "invalid_sort", f"sort must be one of: {', '.join(s.key for s in allowed)}."
    )


def parse_limit(value: int | None) -> int:
    if value is None:
        return DEFAULT_LIMIT
    if not 1 <= value <= MAX_LIMIT:
        raise InvalidQueryParameterError(
            "limit", "invalid_limit", f"limit must be between 1 and {MAX_LIMIT}."
        )
    return value


def encode_cursor(sort: SortSpec, value: CursorValue, row_id: UUID) -> str:
    raw = value.isoformat() if isinstance(value, datetime) else str(value)
    payload = json.dumps({"v": CURSOR_VERSION, "s": sort.key, "k": [raw, str(row_id)]}, separators=(",", ":"))
    return base64.urlsafe_b64encode(payload.encode("utf-8")).rstrip(b"=").decode("ascii")


def _invalid_cursor() -> InvalidQueryParameterError:
    return InvalidQueryParameterError("cursor", "invalid_cursor", "cursor is not valid for this list.")


def decode_cursor(value: str | None, sort: SortSpec) -> Cursor | None:
    """Parse a client cursor; anything malformed, or made for another sort, is rejected."""
    if value is None:
        return None
    if not value or len(value) > MAX_CURSOR_LENGTH:
        raise _invalid_cursor()
    try:
        padded = value + "=" * (-len(value) % 4)
        data = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, binascii.Error, UnicodeError):
        raise _invalid_cursor() from None
    if not isinstance(data, dict) or data.get("v") != CURSOR_VERSION or data.get("s") != sort.key:
        raise _invalid_cursor()
    key = data.get("k")
    if not isinstance(key, list) or len(key) != 2 or not all(isinstance(part, str) for part in key):
        raise _invalid_cursor()
    raw_value, raw_id = key
    try:
        row_id = UUID(raw_id)
        cursor_value: CursorValue
        if sort.value_type is datetime:
            cursor_value = datetime.fromisoformat(raw_value)
        elif sort.value_type is int:
            if not raw_value.isascii() or not raw_value.lstrip("-").isdigit() or len(raw_value) > 12:
                raise ValueError(raw_value)
            cursor_value = int(raw_value)
        else:
            cursor_value = raw_value
    except ValueError:
        raise _invalid_cursor() from None
    if isinstance(cursor_value, datetime) and cursor_value.tzinfo is None:
        raise _invalid_cursor()
    return Cursor(value=cursor_value, id=row_id)


def escape_like(value: str) -> str:
    """Escape LIKE wildcards; queries use ``ESCAPE '\\'``."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
