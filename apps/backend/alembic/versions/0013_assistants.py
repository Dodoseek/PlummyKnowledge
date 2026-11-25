"""Assistants and their knowledge bases (DATABASE_SCHEMA.md §5.3, ADR-005).

There is no access table: who may use an assistant follows from the access to every one of its knowledge
bases. Deleting a knowledge base removes its links here (the application does it with the soft delete;
``ON DELETE CASCADE`` covers a hard delete). An embedding profile in use cannot be deleted. A chat model is
refused for deletion by the application while a live assistant uses it; a deleted assistant loses it
(``SET NULL``), so it never blocks the model. Only deleted assistants have no model (CHECK).

Revision ID: 0013
Revises: 0012
Create Date: 2026-11-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0013"
down_revision: str | None = "0012"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def _ts(name: str, *, nullable: bool = True, default_now: bool = False) -> sa.Column[object]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=sa.text("now()") if default_now else None,
        nullable=nullable,
    )


def _ck(name: str, condition: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(condition, name=op.f(f"ck_assistants_{name}"))


def upgrade() -> None:
    op.create_table(
        "assistants",
        sa.Column("id", UUID, nullable=False),
        sa.Column("name", postgresql.CITEXT(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("instructions", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("model_config_id", UUID, nullable=True),
        sa.Column("embedding_config_id", UUID, nullable=False),
        sa.Column(
            "retrieval_settings", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("max_context_tokens", sa.Integer(), server_default=sa.text("8000"), nullable=False),
        sa.Column("max_history_messages", sa.Integer(), server_default=sa.text("10"), nullable=False),
        sa.Column("max_output_tokens", sa.Integer(), server_default=sa.text("1024"), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("created_by", UUID, nullable=True),
        _ts("created_at", nullable=False, default_now=True),
        _ts("updated_at", nullable=False, default_now=True),
        _ts("deleted_at"),
        _ck("name_length", "char_length(name) BETWEEN 1 AND 200"),
        _ck("description_length", "description IS NULL OR char_length(description) <= 2000"),
        _ck("instructions_length", "char_length(instructions) <= 20000"),
        _ck("retrieval_settings_object", "jsonb_typeof(retrieval_settings) = 'object'"),
        _ck("max_context_tokens", "max_context_tokens BETWEEN 256 AND 128000"),
        _ck("max_history_messages", "max_history_messages BETWEEN 0 AND 100"),
        _ck("max_output_tokens", "max_output_tokens BETWEEN 1 AND 128000"),
        _ck("status", "status IN ('active', 'disabled')"),
        _ck("model_config", "model_config_id IS NOT NULL OR deleted_at IS NOT NULL"),
        sa.ForeignKeyConstraint(
            ["model_config_id"],
            ["model_configs.id"],
            name=op.f("fk_assistants_model_config_id_model_configs"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["embedding_config_id"],
            ["embedding_provider_configs.id"],
            name=op.f("fk_assistants_embedding_config_id_embedding_provider_configs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_assistants_created_by_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_assistants")),
    )
    op.create_index(
        "uq_assistants_name_active",
        "assistants",
        ["name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index("ix_assistants_created_at_id", "assistants", ["created_at", "id"])
    op.create_index("ix_assistants_model_config_id", "assistants", ["model_config_id"])
    op.create_index("ix_assistants_embedding_config_id", "assistants", ["embedding_config_id"])

    op.create_table(
        "assistant_knowledge_bases",
        sa.Column("assistant_id", UUID, nullable=False),
        sa.Column("knowledge_base_id", UUID, nullable=False),
        _ts("created_at", nullable=False, default_now=True),
        sa.ForeignKeyConstraint(
            ["assistant_id"],
            ["assistants.id"],
            name=op.f("fk_assistant_knowledge_bases_assistant_id_assistants"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id"],
            ["knowledge_bases.id"],
            name=op.f("fk_assistant_knowledge_bases_knowledge_base_id_knowledge_bases"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "assistant_id", "knowledge_base_id", name=op.f("pk_assistant_knowledge_bases")
        ),
    )
    op.create_index(
        "ix_assistant_knowledge_bases_knowledge_base_id", "assistant_knowledge_bases", ["knowledge_base_id"]
    )


def downgrade() -> None:
    op.drop_table("assistant_knowledge_bases")
    op.drop_table("assistants")
