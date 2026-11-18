"""AI providers, chat model configs and embedding profiles (DATABASE_SCHEMA.md §4.2-4.4).

API keys live in ``secret_references`` (ADR-016); a provider only holds the reference.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)


def _timestamps() -> list[sa.Column[object]]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def upgrade() -> None:
    op.create_table(
        "ai_providers",
        sa.Column("id", UUID, nullable=False),
        sa.Column("name", postgresql.CITEXT(), nullable=False),
        sa.Column("provider_type", sa.Text(), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=False),
        sa.Column("secret_ref_id", UUID, nullable=True),
        sa.Column("settings", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("last_test_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_test_status", sa.Text(), nullable=True),
        sa.Column("last_test_error", sa.Text(), nullable=True),
        *_timestamps(),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "provider_type IN ('openai', 'anthropic', 'ollama', 'openai_compatible')",
            name=op.f("ck_ai_providers_provider_type"),
        ),
        sa.CheckConstraint("char_length(name) BETWEEN 1 AND 100", name=op.f("ck_ai_providers_name_length")),
        sa.CheckConstraint(
            "char_length(base_url) BETWEEN 1 AND 2048", name=op.f("ck_ai_providers_base_url_length")
        ),
        sa.CheckConstraint(
            "last_test_status IS NULL OR last_test_status IN ('ok', 'failed')",
            name=op.f("ck_ai_providers_last_test_status"),
        ),
        sa.CheckConstraint(
            "last_test_error IS NULL OR last_test_error ~ '^[a-z_]{1,64}$'",
            name=op.f("ck_ai_providers_last_test_error_code"),
        ),
        sa.CheckConstraint("jsonb_typeof(settings) = 'object'", name=op.f("ck_ai_providers_settings_object")),
        sa.ForeignKeyConstraint(
            ["secret_ref_id"],
            ["secret_references.id"],
            name=op.f("fk_ai_providers_secret_ref_id_secret_references"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ai_providers")),
    )
    op.create_index(
        "uq_ai_providers_name_active",
        "ai_providers",
        ["name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index("ix_ai_providers_created_at_id", "ai_providers", ["created_at", "id"])
    op.create_index("uq_ai_providers_secret_ref_id", "ai_providers", ["secret_ref_id"], unique=True)

    op.create_table(
        "model_configs",
        sa.Column("id", UUID, nullable=False),
        sa.Column("provider_id", UUID, nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), server_default=sa.text("'chat'"), nullable=False),
        sa.Column("context_window", sa.Integer(), nullable=True),
        sa.Column("max_output_tokens", sa.Integer(), nullable=True),
        sa.Column(
            "default_params", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
        sa.Column("capabilities", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        *_timestamps(),
        sa.CheckConstraint("kind = 'chat'", name=op.f("ck_model_configs_kind")),
        sa.CheckConstraint(
            "char_length(model_id) BETWEEN 1 AND 256", name=op.f("ck_model_configs_model_id_length")
        ),
        sa.CheckConstraint(
            "display_name IS NULL OR char_length(display_name) <= 200",
            name=op.f("ck_model_configs_display_name_length"),
        ),
        sa.CheckConstraint(
            "context_window IS NULL OR context_window > 0", name=op.f("ck_model_configs_context_window")
        ),
        sa.CheckConstraint(
            "max_output_tokens IS NULL OR max_output_tokens > 0",
            name=op.f("ck_model_configs_max_output_tokens"),
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["ai_providers.id"],
            name=op.f("fk_model_configs_provider_id_ai_providers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_model_configs")),
        sa.UniqueConstraint("provider_id", "model_id", name=op.f("uq_model_configs_provider_id_model_id")),
    )

    op.create_table(
        "embedding_provider_configs",
        sa.Column("id", UUID, nullable=False),
        sa.Column("name", postgresql.CITEXT(), nullable=False),
        sa.Column("provider_id", UUID, nullable=False),
        sa.Column("model_id", sa.Text(), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("distance_metric", sa.Text(), server_default=sa.text("'cosine'"), nullable=False),
        sa.Column("normalize", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("max_input_tokens", sa.Integer(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        *_timestamps(),
        sa.CheckConstraint(
            "char_length(name) BETWEEN 1 AND 100", name=op.f("ck_embedding_provider_configs_name_length")
        ),
        sa.CheckConstraint(
            "char_length(model_id) BETWEEN 1 AND 256",
            name=op.f("ck_embedding_provider_configs_model_id_length"),
        ),
        sa.CheckConstraint(
            "dimensions BETWEEN 1 AND 16000", name=op.f("ck_embedding_provider_configs_dimensions")
        ),
        sa.CheckConstraint(
            "distance_metric IN ('cosine', 'inner_product', 'l2')",
            name=op.f("ck_embedding_provider_configs_distance_metric"),
        ),
        sa.CheckConstraint(
            "max_input_tokens IS NULL OR max_input_tokens > 0",
            name=op.f("ck_embedding_provider_configs_max_input_tokens"),
        ),
        sa.CheckConstraint(
            "status IN ('active', 'deprecated', 'retired')", name=op.f("ck_embedding_provider_configs_status")
        ),
        sa.ForeignKeyConstraint(
            ["provider_id"],
            ["ai_providers.id"],
            name=op.f("fk_embedding_provider_configs_provider_id_ai_providers"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_embedding_provider_configs")),
        sa.UniqueConstraint("name", name=op.f("uq_embedding_provider_configs_name")),
    )
    op.create_index(
        "ix_embedding_provider_configs_provider_id", "embedding_provider_configs", ["provider_id"]
    )
    op.create_index(
        "ix_embedding_provider_configs_created_at_id", "embedding_provider_configs", ["created_at", "id"]
    )


def downgrade() -> None:
    op.drop_index("ix_embedding_provider_configs_created_at_id", table_name="embedding_provider_configs")
    op.drop_index("ix_embedding_provider_configs_provider_id", table_name="embedding_provider_configs")
    op.drop_table("embedding_provider_configs")
    op.drop_table("model_configs")
    op.drop_index("uq_ai_providers_secret_ref_id", table_name="ai_providers")
    op.drop_index("ix_ai_providers_created_at_id", table_name="ai_providers")
    op.drop_index("uq_ai_providers_name_active", table_name="ai_providers")
    op.drop_table("ai_providers")
