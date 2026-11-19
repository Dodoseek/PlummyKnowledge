"""Column types the base SQLAlchemy dialect does not know.

``vector`` (pgvector) is declared here instead of adding the ``pgvector`` package: the application writes
vectors with explicit SQL casts (ADR-017) and only needs the type for the schema and for reflection.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.dialects.postgresql.base import ischema_names
from sqlalchemy.types import UserDefinedType


class Vector(UserDefinedType[Any]):
    """pgvector ``vector`` without a fixed dimension (ADR-017 §1)."""

    cache_ok = True

    def get_col_spec(self, **kw: Any) -> str:
        return "VECTOR"


# Reflection (Alembic's schema comparison) maps the database type back to this class instead of warning.
ischema_names["vector"] = Vector
