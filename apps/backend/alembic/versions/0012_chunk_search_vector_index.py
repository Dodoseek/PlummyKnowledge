"""GIN index on ``document_chunks.search_vector`` (ADR-018), built concurrently.

``CREATE INDEX CONCURRENTLY`` cannot run in a transaction, so it runs in an autocommit block and does not
block chunk writes while it builds. An interrupted concurrent build leaves an invalid index behind; it is
dropped first so the upgrade can be repeated.

Revision ID: 0012
Revises: 0011
Create Date: 2026-11-25
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0012"
down_revision: str | None = "0011"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INDEX = "ix_document_chunks_search_vector"
# A constant statement: no value from outside the migration reaches it.
DROP_INVALID = """
DO $$ BEGIN
  IF EXISTS (
    SELECT 1 FROM pg_class c
    JOIN pg_index i ON i.indexrelid = c.oid
    JOIN pg_namespace n ON n.oid = c.relnamespace
    WHERE c.relname = 'ix_document_chunks_search_vector' AND n.nspname = current_schema() AND NOT i.indisvalid
  ) THEN
    DROP INDEX ix_document_chunks_search_vector;
  END IF;
END $$
"""


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(DROP_INVALID)
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {INDEX} ON document_chunks USING gin (search_vector)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {INDEX}")
