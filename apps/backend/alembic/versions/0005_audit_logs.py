"""Append-only audit log (DATABASE_SCHEMA.md §7.3).

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "audit_logs",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("actor_type", sa.Text(), nullable=False),
        sa.Column("actor_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("target_type", sa.Text(), nullable=True),
        sa.Column("target_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=False),
        sa.Column("correlation_id", sa.Text(), nullable=True),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("details", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.CheckConstraint(
            "actor_type IN ('user', 'anonymous', 'system', 'worker')", name=op.f("ck_audit_logs_actor_type")
        ),
        sa.CheckConstraint("outcome IN ('success', 'denied', 'failure')", name=op.f("ck_audit_logs_outcome")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_logs")),
    )
    op.create_index("ix_audit_logs_created_at", "audit_logs", [sa.text("created_at DESC")])
    op.create_index("ix_audit_logs_action_created_at", "audit_logs", ["action", "created_at"])
    op.create_index("ix_audit_logs_target", "audit_logs", ["target_type", "target_id"])
    op.create_index("ix_audit_logs_actor_user_id_created_at", "audit_logs", ["actor_user_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_audit_logs_actor_user_id_created_at", table_name="audit_logs")
    op.drop_index("ix_audit_logs_target", table_name="audit_logs")
    op.drop_index("ix_audit_logs_action_created_at", table_name="audit_logs")
    op.drop_index("ix_audit_logs_created_at", table_name="audit_logs")
    op.drop_table("audit_logs")
