"""Server-side sessions (DATABASE_SCHEMA.md §3.5, ADR-013).

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("token_hash", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoke_reason", sa.Text(), nullable=True),
        sa.Column("user_agent", sa.Text(), nullable=True),
        sa.Column("ip_address", postgresql.INET(), nullable=True),
        sa.CheckConstraint(
            "revoke_reason IN ('logout', 'superseded', 'admin', 'password_change', 'user_disabled')",
            name=op.f("ck_sessions_revoke_reason"),
        ),
        sa.CheckConstraint(
            "(revoked_at IS NULL) = (revoke_reason IS NULL)", name=op.f("ck_sessions_revoked_consistency")
        ),
        sa.CheckConstraint("octet_length(token_hash) = 32", name=op.f("ck_sessions_token_hash_length")),
        sa.CheckConstraint("char_length(user_agent) <= 512", name=op.f("ck_sessions_user_agent_length")),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_sessions_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sessions")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_sessions_token_hash")),
    )
    op.create_index(
        "ix_sessions_user_id_active", "sessions", ["user_id"], postgresql_where=sa.text("revoked_at IS NULL")
    )
    op.create_index("ix_sessions_expires_at", "sessions", ["expires_at"])


def downgrade() -> None:
    op.drop_index("ix_sessions_expires_at", table_name="sessions")
    op.drop_index("ix_sessions_user_id_active", table_name="sessions")
    op.drop_table("sessions")
