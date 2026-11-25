"""Keyset pagination of ORM queries (ADR-014)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from sqlalchemy import Select, literal, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from plummy.application.pagination import Page, PageRequest, encode_cursor
from plummy.infrastructure.db.base import Base


async def paginate[M: Base, D](
    db: AsyncSession,
    statement: Select[M],
    model: type[M],
    page: PageRequest,
    mapper: Callable[[M], D],
) -> Page[D]:
    """Keyset pagination over ``(sort column, id)`` (ADR-014)."""
    column: Any = getattr(model, page.sort.field)
    row_id: Any = getattr(model, "id")  # noqa: B009 - every paginated model has ``id``
    if page.after is not None:
        # Bind the cursor value with the column's type (e.g. citext) so it compares like the ORDER BY.
        key = tuple_(literal(page.after.value, type_=column.type), literal(page.after.id, type_=row_id.type))
        position = tuple_(column, row_id)
        statement = statement.where(position < key if page.sort.descending else position > key)
    order = (column.desc(), row_id.desc()) if page.sort.descending else (column.asc(), row_id.asc())
    rows = list((await db.scalars(statement.order_by(*order).limit(page.limit + 1))).all())
    next_cursor = None
    if len(rows) > page.limit:
        rows = rows[: page.limit]
        last = rows[-1]
        next_cursor = encode_cursor(page.sort, getattr(last, page.sort.field), getattr(last, "id"))  # noqa: B009
    return Page(items=[mapper(row) for row in rows], next_cursor=next_cursor)
