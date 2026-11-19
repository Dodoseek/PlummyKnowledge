"""``ingestion_jobs.updated_at``: the maintenance sweep re-publishes queued jobs that were never sent.

A job is ``queued`` without ``celery_task_id`` while it waits for the broker (a failed publish after upload,
hand-off or retry). The partial index keeps the periodic lookup cheap.

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "ingestion_jobs",
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index(
        "ix_ingestion_jobs_unpublished",
        "ingestion_jobs",
        ["updated_at"],
        postgresql_where=sa.text("status = 'queued' AND celery_task_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_ingestion_jobs_unpublished", table_name="ingestion_jobs")
    op.drop_column("ingestion_jobs", "updated_at")
