"""``document_chunks.search_vector``: full-text search of chunks (ADR-018).

A stored generated column: PostgreSQL computes it from ``content`` with the built-in ``russian``
configuration (Cyrillic words by russian_stem, Latin words by english_stem), the application never writes
it. Adding the column rewrites the table. The GIN index is built concurrently by revision 0012.

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import TSVECTOR

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SEARCH_VECTOR = "to_tsvector('russian'::regconfig, content)"


def upgrade() -> None:
    op.add_column(
        "document_chunks",
        sa.Column("search_vector", TSVECTOR(), sa.Computed(SEARCH_VECTOR, persisted=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("document_chunks", "search_vector")
