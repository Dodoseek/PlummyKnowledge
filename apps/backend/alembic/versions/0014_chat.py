"""Conversations, messages, citations and generation runs (DATABASE_SCHEMA.md §6, ADR-019).

A conversation belongs to one user and one assistant. Assistants are only soft-deleted, so the link is
``RESTRICT``; deleting a user (purge) takes the conversations with it. Citations are a snapshot: they keep the
document, version and chunk IDs without foreign keys, so purging a document does not touch history, and opening
a citation checks access again. A generation run keeps the model and profile it used; deleting either does not
block on history (``SET NULL``). Prompts and retrieved text are never stored.

Revision ID: 0014
Revises: 0013
Create Date: 2026-11-25
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0014"
down_revision: str | None = "0013"
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


def _ck(table: str, name: str, condition: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(condition, name=op.f(f"ck_{table}_{name}"))


def _fk(table: str, column: str, target: str, ondelete: str) -> sa.ForeignKeyConstraint:
    return sa.ForeignKeyConstraint(
        [column], [f"{target}.id"], name=op.f(f"fk_{table}_{column}_{target}"), ondelete=ondelete
    )


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", UUID, nullable=False),
        sa.Column("user_id", UUID, nullable=False),
        sa.Column("assistant_id", UUID, nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        _ts("created_at", nullable=False, default_now=True),
        _ts("updated_at", nullable=False, default_now=True),
        _ts("last_message_at", nullable=False, default_now=True),
        _ts("deleted_at"),
        _ck("conversations", "title_length", "char_length(title) BETWEEN 1 AND 200"),
        _fk("conversations", "user_id", "users", "CASCADE"),
        _fk("conversations", "assistant_id", "assistants", "RESTRICT"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
    )
    op.create_index(
        "ix_conversations_user_id_last_message_at",
        "conversations",
        ["user_id", sa.text("last_message_at DESC"), sa.text("id DESC")],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index("ix_conversations_assistant_id", "conversations", ["assistant_id"])
    op.create_index(
        "ix_conversations_deleted_at",
        "conversations",
        ["deleted_at"],
        postgresql_where=sa.text("deleted_at IS NOT NULL"),
    )

    op.create_table(
        "messages",
        sa.Column("id", UUID, nullable=False),
        sa.Column("conversation_id", UUID, nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("role", sa.Text(), nullable=False),
        sa.Column("content", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("client_message_id", UUID, nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        _ts("created_at", nullable=False, default_now=True),
        _ts("completed_at"),
        _ck("messages", "sequence", "sequence >= 1"),
        _ck("messages", "role", "role IN ('user', 'assistant')"),
        _ck("messages", "status", "status IN ('completed', 'streaming', 'stopped', 'failed')"),
        _ck("messages", "content_length", "char_length(content) <= 200000"),
        # Only a user's message is sent by the client, and it always carries its idempotency key.
        _ck("messages", "client_message_id", "(role = 'user') = (client_message_id IS NOT NULL)"),
        _fk("messages", "conversation_id", "conversations", "CASCADE"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
        sa.UniqueConstraint("conversation_id", "sequence", name=op.f("uq_messages_conversation_id_sequence")),
        sa.UniqueConstraint(
            "conversation_id", "client_message_id", name=op.f("uq_messages_conversation_id_client_message_id")
        ),
    )
    op.create_index(
        "ix_messages_streaming",
        "messages",
        ["created_at"],
        postgresql_where=sa.text("status = 'streaming'"),
    )

    op.create_table(
        "message_citations",
        sa.Column("message_id", UUID, nullable=False),
        sa.Column("citation_key", sa.Text(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("document_id", UUID, nullable=False),
        sa.Column("document_version_id", UUID, nullable=False),
        sa.Column("chunk_id", UUID, nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("page", sa.Integer(), nullable=True),
        sa.Column("section", sa.Text(), nullable=True),
        sa.Column("sheet", sa.Text(), nullable=True),
        sa.Column("row_index", sa.Integer(), nullable=True),
        sa.Column("snippet", sa.Text(), nullable=False),
        sa.Column("viewer_route", sa.Text(), nullable=False),
        _ck("message_citations", "citation_key", "citation_key ~ '^c[0-9]{1,4}$'"),
        _ck("message_citations", "ordinal", "ordinal >= 1"),
        _fk("message_citations", "message_id", "messages", "CASCADE"),
        sa.PrimaryKeyConstraint("message_id", "citation_key", name=op.f("pk_message_citations")),
        sa.UniqueConstraint("message_id", "ordinal", name=op.f("uq_message_citations_message_id_ordinal")),
    )

    op.create_table(
        "generation_runs",
        sa.Column("id", UUID, nullable=False),
        sa.Column("conversation_id", UUID, nullable=False),
        sa.Column("message_id", UUID, nullable=False),
        sa.Column("assistant_id", UUID, nullable=False),
        sa.Column("model_config_id", UUID, nullable=True),
        sa.Column("embedding_config_id", UUID, nullable=True),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("finish_reason", sa.Text(), nullable=True),
        sa.Column(
            "retrieved_chunk_ids",
            postgresql.ARRAY(UUID),
            server_default=sa.text("'{}'::uuid[]"),
            nullable=False,
        ),
        sa.Column("rejected_citation_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), nullable=True),
        sa.Column("completion_tokens", sa.Integer(), nullable=True),
        sa.Column("retrieval_ms", sa.Integer(), nullable=True),
        sa.Column("time_to_first_token_ms", sa.Integer(), nullable=True),
        sa.Column("total_ms", sa.Integer(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        _ts("started_at", nullable=False, default_now=True),
        _ts("finished_at"),
        _ck("generation_runs", "status", "status IN ('running', 'completed', 'stopped', 'failed')"),
        _ck(
            "generation_runs",
            "finish_reason",
            "finish_reason IS NULL OR finish_reason IN ('stop', 'length', 'user_cancelled', 'error')",
        ),
        _ck("generation_runs", "counters", "rejected_citation_count >= 0"),
        _fk("generation_runs", "conversation_id", "conversations", "CASCADE"),
        _fk("generation_runs", "message_id", "messages", "CASCADE"),
        _fk("generation_runs", "assistant_id", "assistants", "RESTRICT"),
        _fk("generation_runs", "model_config_id", "model_configs", "SET NULL"),
        sa.ForeignKeyConstraint(
            ["embedding_config_id"],
            ["embedding_provider_configs.id"],
            name=op.f("fk_generation_runs_embedding_config_id_embedding_provider_configs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_generation_runs")),
        sa.UniqueConstraint("message_id", name=op.f("uq_generation_runs_message_id")),
    )
    op.create_index("ix_generation_runs_conversation_id", "generation_runs", ["conversation_id"])
    op.create_index("ix_generation_runs_assistant_id", "generation_runs", ["assistant_id"])
    op.create_index("ix_generation_runs_model_config_id", "generation_runs", ["model_config_id"])
    op.create_index("ix_generation_runs_embedding_config_id", "generation_runs", ["embedding_config_id"])


def downgrade() -> None:
    op.drop_table("generation_runs")
    op.drop_table("message_citations")
    op.drop_table("messages")
    op.drop_table("conversations")
