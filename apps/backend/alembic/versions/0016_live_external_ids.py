"""External IDs are unique among the live documents of a source (ADR-020 §8).

A file deleted in the source and later restored there (or moved out of the synced folder and back) gets a new
document while the old, soft-deleted one waits for its purge; ``UNIQUE (data_source_id, external_id)`` on all
rows would refuse it. The constraint becomes a partial unique index on live documents; it also serves the
lookup of a document by its source ID during a sync.

Revision ID: 0016
Revises: 0015
Create Date: 2026-11-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0016"
down_revision: str | None = "0015"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(op.f("uq_documents_data_source_id_external_id"), "documents", type_="unique")
    op.create_index(
        "uq_documents_live_external_id",
        "documents",
        ["data_source_id", "external_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    # Fails while a deleted and a live document share an external ID: purge the deleted ones first.
    op.drop_index("uq_documents_live_external_id", table_name="documents")
    op.create_unique_constraint(
        op.f("uq_documents_data_source_id_external_id"), "documents", ["data_source_id", "external_id"]
    )
