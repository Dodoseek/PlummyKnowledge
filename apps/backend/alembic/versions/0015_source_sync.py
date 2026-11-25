"""External document sources: sync state, OAuth credentials and sync jobs.

DATABASE_SCHEMA.md §5.4, §5.5, §7.2 and ADR-020. A source belongs to exactly one knowledge base (``UNIQUE`` on
``knowledge_base_sources.data_source_id``; upload sources already did). Credentials hold only a reference to
the encrypted refresh token; the access token is never stored. At most one sync job of a source is queued
or running at a time.

Revision ID: 0015
Revises: 0014
Create Date: 2026-11-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0015"
down_revision: str | None = "0014"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JOB_STATUSES = "'queued', 'running', 'succeeded', 'failed', 'cancelled', 'dead'"
ERROR_CODE_FORMAT = "~ '^[a-z_]{1,64}$'"  # typed codes only, never file names or provider output


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
    op.add_column("data_sources", sa.Column("sync_cursor", sa.Text(), nullable=True))
    op.add_column("data_sources", sa.Column("sync_interval_minutes", sa.Integer(), nullable=True))
    op.add_column("data_sources", _ts("next_sync_at"))
    op.add_column("data_sources", _ts("last_sync_at"))
    op.add_column("data_sources", _ts("last_reconciled_at"))
    op.add_column("data_sources", sa.Column("last_sync_status", sa.Text(), nullable=True))
    op.add_column("data_sources", sa.Column("last_error", sa.Text(), nullable=True))
    op.create_check_constraint(
        op.f("ck_data_sources_last_error_format"),
        "data_sources",
        f"last_error IS NULL OR last_error {ERROR_CODE_FORMAT}",
    )
    op.create_check_constraint(
        op.f("ck_data_sources_sync_interval"),
        "data_sources",
        "sync_interval_minutes IS NULL OR sync_interval_minutes BETWEEN 5 AND 1440",
    )
    op.create_check_constraint(
        op.f("ck_data_sources_last_sync_status"),
        "data_sources",
        "last_sync_status IS NULL OR last_sync_status IN ('succeeded', 'failed', 'cancelled')",
    )
    op.create_check_constraint(
        op.f("ck_data_sources_cursor_length"),
        "data_sources",
        "sync_cursor IS NULL OR char_length(sync_cursor) <= 2048",
    )
    op.create_index(
        "ix_data_sources_next_sync_at",
        "data_sources",
        ["next_sync_at"],
        postgresql_where=sa.text("status = 'active' AND deleted_at IS NULL AND next_sync_at IS NOT NULL"),
    )

    op.drop_index("ix_knowledge_base_sources_data_source_id", table_name="knowledge_base_sources")
    op.create_unique_constraint(
        op.f("uq_knowledge_base_sources_data_source_id"), "knowledge_base_sources", ["data_source_id"]
    )

    op.create_table(
        "data_source_credentials",
        sa.Column("data_source_id", UUID, nullable=False),
        sa.Column("secret_ref_id", UUID, nullable=False),
        sa.Column("account_label", sa.Text(), nullable=True),
        sa.Column("granted_scopes", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        _ts("created_at", nullable=False, default_now=True),
        _ts("updated_at", nullable=False, default_now=True),
        _ts("last_refreshed_at"),
        _ts("revoked_at"),
        sa.Column("connected_by", UUID, nullable=True),
        _ck("data_source_credentials", "status", "status IN ('valid', 'expired', 'revoked')"),
        _ck(
            "data_source_credentials",
            "account_label_length",
            "account_label IS NULL OR char_length(account_label) <= 320",
        ),
        _ck(
            "data_source_credentials",
            "revoked_consistent",
            "(status = 'revoked') = (revoked_at IS NOT NULL)",
        ),
        _fk("data_source_credentials", "data_source_id", "data_sources", "CASCADE"),
        _fk("data_source_credentials", "secret_ref_id", "secret_references", "RESTRICT"),
        _fk("data_source_credentials", "connected_by", "users", "SET NULL"),
        sa.PrimaryKeyConstraint("data_source_id", name=op.f("pk_data_source_credentials")),
        sa.UniqueConstraint("secret_ref_id", name=op.f("uq_data_source_credentials_secret_ref_id")),
    )

    op.create_table(
        "sync_jobs",
        sa.Column("id", UUID, nullable=False),
        sa.Column("data_source_id", UUID, nullable=False),
        sa.Column("trigger", sa.Text(), nullable=False),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("cursor_before", sa.Text(), nullable=True),
        sa.Column("cursor_after", sa.Text(), nullable=True),
        sa.Column("stats", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("attempt", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("5"), nullable=False),
        sa.Column("celery_task_id", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("requested_by", UUID, nullable=True),
        _ts("queued_at", nullable=False, default_now=True),
        _ts("started_at"),
        _ts("finished_at"),
        _ts("updated_at", nullable=False, default_now=True),
        _ck("sync_jobs", "trigger", "trigger IN ('manual', 'schedule', 'reconciliation', 'initial')"),
        _ck("sync_jobs", "mode", "mode IN ('initial', 'incremental', 'reconciliation')"),
        _ck("sync_jobs", "status", f"status IN ({JOB_STATUSES})"),
        _ck("sync_jobs", "stats_object", "jsonb_typeof(stats) = 'object'"),
        _ck("sync_jobs", "attempts", "attempt >= 0 AND max_attempts BETWEEN 1 AND 20"),
        _ck("sync_jobs", "error_code_format", f"error_code IS NULL OR error_code {ERROR_CODE_FORMAT}"),
        _ck(
            "sync_jobs", "error_message_length", "error_message IS NULL OR char_length(error_message) <= 500"
        ),
        _fk("sync_jobs", "data_source_id", "data_sources", "CASCADE"),
        _fk("sync_jobs", "requested_by", "users", "SET NULL"),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sync_jobs")),
    )
    op.create_index(
        "sync_jobs_one_active_per_source",
        "sync_jobs",
        ["data_source_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_index(
        "ix_sync_jobs_data_source_id_queued_at",
        "sync_jobs",
        ["data_source_id", sa.text("queued_at DESC"), sa.text("id DESC")],
    )
    op.create_index(
        "ix_sync_jobs_status_updated_at",
        "sync_jobs",
        ["status", "updated_at"],
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )


def downgrade() -> None:
    op.drop_table("sync_jobs")
    op.drop_table("data_source_credentials")
    op.drop_constraint(
        op.f("uq_knowledge_base_sources_data_source_id"), "knowledge_base_sources", type_="unique"
    )
    op.create_index("ix_knowledge_base_sources_data_source_id", "knowledge_base_sources", ["data_source_id"])
    op.drop_index("ix_data_sources_next_sync_at", table_name="data_sources")
    for name in ("cursor_length", "last_sync_status", "sync_interval", "last_error_format"):
        op.drop_constraint(op.f(f"ck_data_sources_{name}"), "data_sources", type_="check")
    for column in (
        "last_error",
        "last_sync_status",
        "last_reconciled_at",
        "last_sync_at",
        "next_sync_at",
        "sync_interval_minutes",
        "sync_cursor",
    ):
        op.drop_column("data_sources", column)
