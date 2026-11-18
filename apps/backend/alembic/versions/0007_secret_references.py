"""Encrypted secrets (ADR-016, DATABASE_SCHEMA.md §4.1).

Only AES-256-GCM ciphertext, nonce and key id are stored; the key ring lives outside the database.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-18
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "secret_references",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("backend", sa.Text(), server_default=sa.text("'db_encrypted'"), nullable=False),
        sa.Column("ciphertext", sa.LargeBinary(), nullable=False),
        sa.Column("nonce", sa.LargeBinary(), nullable=False),
        sa.Column("key_id", sa.Text(), nullable=False),
        sa.Column("algorithm", sa.Text(), nullable=False),
        sa.Column("hint", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('ai_api_key', 'oauth_refresh_token', 'oauth_client_secret')",
            name=op.f("ck_secret_references_kind"),
        ),
        sa.CheckConstraint("backend = 'db_encrypted'", name=op.f("ck_secret_references_backend")),
        sa.CheckConstraint("algorithm = 'AES-256-GCM'", name=op.f("ck_secret_references_algorithm")),
        sa.CheckConstraint("octet_length(nonce) = 12", name=op.f("ck_secret_references_nonce_length")),
        sa.CheckConstraint(
            "octet_length(ciphertext) > 16", name=op.f("ck_secret_references_ciphertext_length")
        ),
        sa.CheckConstraint(
            "key_id ~ '^[A-Za-z0-9._-]{1,32}$'", name=op.f("ck_secret_references_key_id_format")
        ),
        sa.CheckConstraint(
            "hint IS NULL OR char_length(hint) = 4", name=op.f("ck_secret_references_hint_length")
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_secret_references")),
    )
    op.create_index("ix_secret_references_key_id", "secret_references", ["key_id"])


def downgrade() -> None:
    op.drop_index("ix_secret_references_key_id", table_name="secret_references")
    op.drop_table("secret_references")
