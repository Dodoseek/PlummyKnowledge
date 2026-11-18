"""Users and the instance setup state (DATABASE_SCHEMA.md §3.0, §3.2; ADR-006, ADR-012).

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", postgresql.CITEXT(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('active', 'disabled', 'invited')", name=op.f("ck_users_status")),
        sa.CheckConstraint(
            "char_length(display_name) BETWEEN 1 AND 200", name=op.f("ck_users_display_name_length")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("email", name=op.f("uq_users_email")),
    )
    op.create_index("ix_users_status", "users", ["status"])

    op.create_table(
        "instance_state",
        sa.Column("id", sa.SmallInteger(), autoincrement=False, nullable=False),
        sa.Column("initialized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("initialized_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("bootstrap_token_hash", sa.LargeBinary(), nullable=True),
        sa.Column("bootstrap_token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bootstrap_token_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("id = 1", name=op.f("ck_instance_state_singleton")),
        sa.CheckConstraint(
            "initialized_at IS NULL OR initialized_by_user_id IS NOT NULL",
            name=op.f("ck_instance_state_initialized_by"),
        ),
        sa.CheckConstraint(
            "bootstrap_token_hash IS NULL OR octet_length(bootstrap_token_hash) = 32",
            name=op.f("ck_instance_state_bootstrap_token_hash_length"),
        ),
        sa.ForeignKeyConstraint(
            ["initialized_by_user_id"],
            ["users.id"],
            name=op.f("fk_instance_state_initialized_by_user_id_users"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_instance_state")),
    )
    # The single row exists from the start: "uninitialized" is an explicit state, not a missing row.
    op.execute("INSERT INTO instance_state (id) VALUES (1)")


def downgrade() -> None:
    op.drop_table("instance_state")
    op.drop_index("ix_users_status", table_name="users")
    op.drop_table("users")
