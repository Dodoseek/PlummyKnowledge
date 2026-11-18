"""Groups, group roles, activation tokens; email unique only among non-deleted users.

DATABASE_SCHEMA.md §3.2-3.4, ADR-015. Downgrade fails if two rows share an email (a deleted user and its
re-created successor); that is intended — such data cannot satisfy the old constraint.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def _now() -> sa.TextClause:
    return sa.text("now()")


def upgrade() -> None:
    op.drop_constraint("uq_users_email", "users", type_="unique")
    op.create_index(
        "uq_users_email_active",
        "users",
        ["email"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        "groups",
        sa.Column("id", UUID, nullable=False),
        sa.Column("name", postgresql.CITEXT(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("char_length(name) BETWEEN 1 AND 100", name=op.f("ck_groups_name_length")),
        sa.CheckConstraint(
            "description IS NULL OR char_length(description) <= 500",
            name=op.f("ck_groups_description_length"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_groups")),
    )
    op.create_index(
        "uq_groups_name_active",
        "groups",
        ["name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    op.create_table(
        "user_groups",
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("group_id", UUID, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_user_groups_user_id_users"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["group_id"], ["groups.id"], name=op.f("fk_user_groups_group_id_groups"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("user_id", "group_id", name=op.f("pk_user_groups")),
    )
    op.create_index("ix_user_groups_group_id", "user_groups", ["group_id"])
    # Keyset pagination by creation time (ADR-014).
    op.create_index("ix_users_created_at_id", "users", ["created_at", "id"])
    op.create_index("ix_groups_created_at_id", "groups", ["created_at", "id"])

    op.create_table(
        "group_roles",
        sa.Column("group_id", UUID, nullable=False),
        sa.Column("role_id", UUID, nullable=False),
        sa.Column("granted_by", UUID, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.ForeignKeyConstraint(
            ["group_id"], ["groups.id"], name=op.f("fk_group_roles_group_id_groups"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["role_id"], ["roles.id"], name=op.f("fk_group_roles_role_id_roles"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["granted_by"], ["users.id"], name=op.f("fk_group_roles_granted_by_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("group_id", "role_id", name=op.f("pk_group_roles")),
    )
    op.create_index("ix_group_roles_role_id", "group_roles", ["role_id"])

    op.create_table(
        "user_activation_tokens",
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("token_hash", sa.LargeBinary(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_by", UUID, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=_now(), nullable=False),
        sa.CheckConstraint(
            "octet_length(token_hash) = 32", name=op.f("ck_user_activation_tokens_token_hash_length")
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_activation_tokens_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_user_activation_tokens_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("user_id", name=op.f("pk_user_activation_tokens")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_user_activation_tokens_token_hash")),
    )


def downgrade() -> None:
    op.drop_table("user_activation_tokens")
    op.drop_index("ix_group_roles_role_id", table_name="group_roles")
    op.drop_table("group_roles")
    op.drop_index("ix_groups_created_at_id", table_name="groups")
    op.drop_index("ix_users_created_at_id", table_name="users")
    op.drop_index("ix_user_groups_group_id", table_name="user_groups")
    op.drop_table("user_groups")
    op.drop_index("uq_groups_name_active", table_name="groups")
    op.drop_table("groups")
    op.drop_index("uq_users_email_active", table_name="users")
    op.create_unique_constraint("uq_users_email", "users", ["email"])
