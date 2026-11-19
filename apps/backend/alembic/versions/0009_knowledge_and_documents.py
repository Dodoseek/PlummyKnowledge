"""Knowledge bases, document sources, documents, chunks, embeddings and ingestion jobs.

DATABASE_SCHEMA.md §5.1-5.10 and §7.1; ADR-008 (one pipeline for all sources), ADR-017 (vectors). The HNSW
index of each embedding profile is created by the application at runtime (ADR-017 §2), not here.

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql
from sqlalchemy.types import UserDefinedType

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UUID = postgresql.UUID(as_uuid=True)
JSON_OBJECT = sa.text("'{}'::jsonb")
KEY_FORMAT = "~ '^[0-9a-f]{32}$'"
ERROR_CODE_FORMAT = "~ '^[a-z_]{1,64}$'"


class _Vector(UserDefinedType[object]):
    cache_ok = True

    def get_col_spec(self, **kw: object) -> str:
        return "VECTOR"


def _ts(name: str, *, nullable: bool = True, default_now: bool = False) -> sa.Column[object]:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=sa.text("now()") if default_now else None,
        nullable=nullable,
    )


def _created_updated() -> list[sa.Column[object]]:
    return [
        _ts("created_at", nullable=False, default_now=True),
        _ts("updated_at", nullable=False, default_now=True),
    ]


def _ck(table: str, name: str, condition: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(condition, name=op.f(f"ck_{table}_{name}"))


def upgrade() -> None:
    op.create_table(
        "knowledge_bases",
        sa.Column("id", UUID, nullable=False),
        sa.Column("name", postgresql.CITEXT(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("embedding_config_id", UUID, nullable=False),
        sa.Column("chunking_settings", postgresql.JSONB(), server_default=JSON_OBJECT, nullable=False),
        sa.Column("created_by", UUID, nullable=True),
        *_created_updated(),
        _ts("deleted_at"),
        _ck("knowledge_bases", "name_length", "char_length(name) BETWEEN 1 AND 200"),
        _ck(
            "knowledge_bases", "description_length", "description IS NULL OR char_length(description) <= 2000"
        ),
        _ck("knowledge_bases", "chunking_settings_object", "jsonb_typeof(chunking_settings) = 'object'"),
        sa.ForeignKeyConstraint(
            ["embedding_config_id"],
            ["embedding_provider_configs.id"],
            name=op.f("fk_knowledge_bases_embedding_config_id_embedding_provider_configs"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"],
            ["users.id"],
            name=op.f("fk_knowledge_bases_created_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_bases")),
    )
    op.create_index(
        "uq_knowledge_bases_name_active",
        "knowledge_bases",
        ["name"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index("ix_knowledge_bases_created_at_id", "knowledge_bases", ["created_at", "id"])
    op.create_index("ix_knowledge_bases_embedding_config_id", "knowledge_bases", ["embedding_config_id"])

    op.create_table(
        "knowledge_base_access",
        sa.Column("id", UUID, nullable=False),
        sa.Column("knowledge_base_id", UUID, nullable=False),
        sa.Column("principal_type", sa.Text(), nullable=False),
        sa.Column("user_id", UUID, nullable=True),
        sa.Column("group_id", UUID, nullable=True),
        sa.Column("access_level", sa.Text(), nullable=False),
        sa.Column("granted_by", UUID, nullable=True),
        _ts("created_at", nullable=False, default_now=True),
        _ck("knowledge_base_access", "principal_type", "principal_type IN ('user', 'group')"),
        _ck("knowledge_base_access", "access_level", "access_level IN ('read', 'manage')"),
        _ck(
            "knowledge_base_access",
            "principal",
            "(principal_type = 'user') = (user_id IS NOT NULL) "
            "AND (principal_type = 'group') = (group_id IS NOT NULL)",
        ),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id"],
            ["knowledge_bases.id"],
            name=op.f("fk_knowledge_base_access_knowledge_base_id_knowledge_bases"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_knowledge_base_access_user_id_users"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["groups.id"],
            name=op.f("fk_knowledge_base_access_group_id_groups"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["granted_by"],
            ["users.id"],
            name=op.f("fk_knowledge_base_access_granted_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_knowledge_base_access")),
        sa.UniqueConstraint(
            "knowledge_base_id", "user_id", name=op.f("uq_knowledge_base_access_knowledge_base_id_user_id")
        ),
        sa.UniqueConstraint(
            "knowledge_base_id", "group_id", name=op.f("uq_knowledge_base_access_knowledge_base_id_group_id")
        ),
    )
    op.create_index("ix_knowledge_base_access_user_id", "knowledge_base_access", ["user_id"])
    op.create_index("ix_knowledge_base_access_group_id", "knowledge_base_access", ["group_id"])

    op.create_table(
        "data_sources",
        sa.Column("id", UUID, nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("owner_knowledge_base_id", UUID, nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'active'"), nullable=False),
        sa.Column("config", postgresql.JSONB(), server_default=JSON_OBJECT, nullable=False),
        sa.Column("created_by", UUID, nullable=True),
        *_created_updated(),
        _ts("deleted_at"),
        _ck("data_sources", "type", "type IN ('upload', 'google_drive', 'microsoft_365')"),
        _ck("data_sources", "status", "status IN ('active', 'paused', 'error', 'disconnected')"),
        _ck("data_sources", "name_length", "char_length(name) BETWEEN 1 AND 200"),
        _ck("data_sources", "config_object", "jsonb_typeof(config) = 'object'"),
        _ck("data_sources", "upload_owner", "(type = 'upload') = (owner_knowledge_base_id IS NOT NULL)"),
        sa.ForeignKeyConstraint(
            ["owner_knowledge_base_id"],
            ["knowledge_bases.id"],
            name=op.f("fk_data_sources_owner_knowledge_base_id_knowledge_bases"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"], name=op.f("fk_data_sources_created_by_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_data_sources")),
        sa.UniqueConstraint("owner_knowledge_base_id", name=op.f("uq_data_sources_owner_knowledge_base_id")),
    )

    op.create_table(
        "knowledge_base_sources",
        sa.Column("knowledge_base_id", UUID, nullable=False),
        sa.Column("data_source_id", UUID, nullable=False),
        _ts("created_at", nullable=False, default_now=True),
        sa.ForeignKeyConstraint(
            ["knowledge_base_id"],
            ["knowledge_bases.id"],
            name=op.f("fk_knowledge_base_sources_knowledge_base_id_knowledge_bases"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["data_source_id"],
            ["data_sources.id"],
            name=op.f("fk_knowledge_base_sources_data_source_id_data_sources"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "knowledge_base_id", "data_source_id", name=op.f("pk_knowledge_base_sources")
        ),
    )
    op.create_index("ix_knowledge_base_sources_data_source_id", "knowledge_base_sources", ["data_source_id"])

    op.create_table(
        "documents",
        sa.Column("id", UUID, nullable=False),
        sa.Column("data_source_id", UUID, nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column("filename", sa.Text(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=True),
        sa.Column("external_path", sa.Text(), nullable=True),
        sa.Column("external_etag", sa.Text(), nullable=True),
        sa.Column("current_version_id", UUID, nullable=True),
        sa.Column("processing_status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        _ts("source_created_at"),
        _ts("source_modified_at"),
        sa.Column("source_metadata", postgresql.JSONB(), server_default=JSON_OBJECT, nullable=False),
        *_created_updated(),
        _ts("deleted_at"),
        sa.Column("deletion_reason", sa.Text(), nullable=True),
        _ts("purged_at"),
        _ck("documents", "external_id_length", "char_length(external_id) BETWEEN 1 AND 512"),
        _ck("documents", "filename_length", "char_length(filename) BETWEEN 1 AND 255"),
        _ck(
            "documents",
            "processing_status",
            "processing_status IN ('pending', 'processing', 'indexed', 'failed', 'skipped')",
        ),
        _ck(
            "documents",
            "deletion_reason",
            "deletion_reason IS NULL OR deletion_reason IN "
            "('user', 'remote_deleted', 'out_of_scope', 'source_deleted', 'kb_deleted')",
        ),
        _ck("documents", "deletion_consistent", "(deleted_at IS NULL) = (deletion_reason IS NULL)"),
        _ck("documents", "source_metadata_object", "jsonb_typeof(source_metadata) = 'object'"),
        sa.ForeignKeyConstraint(
            ["data_source_id"],
            ["data_sources.id"],
            name=op.f("fk_documents_data_source_id_data_sources"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_documents")),
        sa.UniqueConstraint(
            "data_source_id", "external_id", name=op.f("uq_documents_data_source_id_external_id")
        ),
    )
    op.create_index(
        "ix_documents_data_source_id_created_at_id", "documents", ["data_source_id", "created_at", "id"]
    )
    op.create_index("ix_documents_processing_status", "documents", ["processing_status"])
    op.create_index("ix_documents_deleted_at", "documents", ["deleted_at"])
    op.create_index("ix_documents_updated_at", "documents", ["updated_at"])

    op.create_table(
        "document_versions",
        sa.Column("id", UUID, nullable=False),
        sa.Column("document_id", UUID, nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("checksum_sha256", sa.LargeBinary(), nullable=False),
        sa.Column("mime_type", sa.Text(), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=True),
        sa.Column("storage_key", sa.Text(), nullable=False),
        sa.Column("extracted_storage_key", sa.Text(), nullable=True),
        sa.Column("external_version", sa.Text(), nullable=True),
        sa.Column("parser", sa.Text(), nullable=True),
        sa.Column("status", sa.Text(), server_default=sa.text("'pending'"), nullable=False),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), server_default=JSON_OBJECT, nullable=False),
        sa.Column("active_chunk_set", sa.Integer(), nullable=True),
        _ts("processed_at"),
        *_created_updated(),
        _ck("document_versions", "version_number", "version_number > 0"),
        _ck("document_versions", "checksum_length", "octet_length(checksum_sha256) = 32"),
        _ck("document_versions", "size_bytes", "size_bytes >= 0"),
        _ck("document_versions", "page_count", "page_count IS NULL OR page_count >= 0"),
        _ck("document_versions", "storage_key_format", f"storage_key {KEY_FORMAT}"),
        _ck(
            "document_versions",
            "extracted_storage_key_format",
            f"extracted_storage_key IS NULL OR extracted_storage_key {KEY_FORMAT}",
        ),
        _ck(
            "document_versions",
            "status",
            "status IN ('pending', 'processing', 'ready', 'failed', 'outdated', 'deleted')",
        ),
        _ck(
            "document_versions", "error_code_format", f"error_code IS NULL OR error_code {ERROR_CODE_FORMAT}"
        ),
        _ck(
            "document_versions",
            "error_message_length",
            "error_message IS NULL OR char_length(error_message) <= 500",
        ),
        _ck("document_versions", "active_chunk_set", "active_chunk_set IS NULL OR active_chunk_set > 0"),
        _ck("document_versions", "metadata_object", "jsonb_typeof(metadata) = 'object'"),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_document_versions_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_versions")),
        sa.UniqueConstraint(
            "document_id", "version_number", name=op.f("uq_document_versions_document_id_version_number")
        ),
        sa.UniqueConstraint(
            "document_id", "checksum_sha256", name=op.f("uq_document_versions_document_id_checksum_sha256")
        ),
    )
    op.create_index("ix_document_versions_status", "document_versions", ["status"])
    op.create_index("ix_document_versions_document_id_status", "document_versions", ["document_id", "status"])
    # documents ↔ document_versions reference each other; the current version is switched in one transaction.
    op.create_foreign_key(
        op.f("fk_documents_current_version_id_document_versions"),
        "documents",
        "document_versions",
        ["current_version_id"],
        ["id"],
        ondelete="SET NULL",
        deferrable=True,
        initially="DEFERRED",
    )

    op.create_table(
        "document_chunks",
        sa.Column("id", UUID, nullable=False),
        sa.Column("document_id", UUID, nullable=False),
        sa.Column("document_version_id", UUID, nullable=False),
        sa.Column("data_source_id", UUID, nullable=False),
        sa.Column("chunk_set", sa.Integer(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_hash", sa.LargeBinary(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=True),
        sa.Column("page_end", sa.Integer(), nullable=True),
        sa.Column("section_path", postgresql.ARRAY(sa.Text()), nullable=True),
        sa.Column("sheet_name", sa.Text(), nullable=True),
        sa.Column("row_start", sa.Integer(), nullable=True),
        sa.Column("row_end", sa.Integer(), nullable=True),
        _ts("created_at", nullable=False, default_now=True),
        _ck("document_chunks", "chunk_set", "chunk_set > 0"),
        _ck("document_chunks", "chunk_index", "chunk_index >= 0"),
        _ck("document_chunks", "content_hash_length", "octet_length(content_hash) = 32"),
        _ck("document_chunks", "token_count", "token_count >= 0"),
        _ck(
            "document_chunks",
            "page_range",
            "page_start IS NULL OR page_end IS NULL OR page_start <= page_end",
        ),
        _ck("document_chunks", "row_range", "row_start IS NULL OR row_end IS NULL OR row_start <= row_end"),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_document_chunks_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_versions.id"],
            name=op.f("fk_document_chunks_document_version_id_document_versions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_document_chunks")),
        sa.UniqueConstraint(
            "document_version_id",
            "chunk_set",
            "chunk_index",
            name=op.f("uq_document_chunks_document_version_id_chunk_set_chunk_index"),
        ),
    )
    op.create_index("ix_document_chunks_data_source_id", "document_chunks", ["data_source_id"])
    op.create_index("ix_document_chunks_document_id", "document_chunks", ["document_id"])

    op.create_table(
        "embeddings",
        sa.Column("id", UUID, nullable=False),
        sa.Column("chunk_id", UUID, nullable=False),
        sa.Column("embedding_config_id", UUID, nullable=False),
        sa.Column("document_version_id", UUID, nullable=False),
        sa.Column("data_source_id", UUID, nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("embedding", _Vector(), nullable=False),
        _ts("created_at", nullable=False, default_now=True),
        _ck("embeddings", "dimensions", "dimensions BETWEEN 1 AND 4000"),
        _ck("embeddings", "embedding_dimensions", "vector_dims(embedding) = dimensions"),
        sa.ForeignKeyConstraint(
            ["chunk_id"],
            ["document_chunks.id"],
            name=op.f("fk_embeddings_chunk_id_document_chunks"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["embedding_config_id"],
            ["embedding_provider_configs.id"],
            name=op.f("fk_embeddings_embedding_config_id_embedding_provider_configs"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_embeddings")),
        sa.UniqueConstraint(
            "chunk_id", "embedding_config_id", name=op.f("uq_embeddings_chunk_id_embedding_config_id")
        ),
    )
    op.create_index(
        "ix_embeddings_config_version", "embeddings", ["embedding_config_id", "document_version_id"]
    )

    op.create_table(
        "ingestion_jobs",
        sa.Column("id", UUID, nullable=False),
        sa.Column("document_id", UUID, nullable=False),
        sa.Column("document_version_id", UUID, nullable=True),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'queued'"), nullable=False),
        sa.Column("stage", sa.Text(), server_default=sa.text("'fetch'"), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("attempt", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default=sa.text("5"), nullable=False),
        sa.Column("celery_task_id", sa.Text(), nullable=True),
        sa.Column("error_code", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("progress", postgresql.JSONB(), server_default=JSON_OBJECT, nullable=False),
        sa.Column("requested_by", UUID, nullable=True),
        _ts("queued_at", nullable=False, default_now=True),
        _ts("started_at"),
        _ts("finished_at"),
        _ck("ingestion_jobs", "kind", "kind IN ('ingest', 'reindex', 'delete', 'purge')"),
        _ck(
            "ingestion_jobs",
            "status",
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'dead')",
        ),
        _ck(
            "ingestion_jobs",
            "stage",
            "stage IN ('fetch', 'authorize', 'validate_type', 'security_check', 'store_original', 'parse', "
            "'normalize', 'chunk', 'embed', 'persist', 'finalize', 'report')",
        ),
        _ck(
            "ingestion_jobs",
            "result",
            "result IS NULL OR result IN ('processed', 'skipped_already_processed', 'skipped_unchanged')",
        ),
        _ck("ingestion_jobs", "attempts", "attempt >= 0 AND max_attempts >= 1"),
        _ck("ingestion_jobs", "error_code_format", f"error_code IS NULL OR error_code {ERROR_CODE_FORMAT}"),
        _ck(
            "ingestion_jobs",
            "error_message_length",
            "error_message IS NULL OR char_length(error_message) <= 500",
        ),
        _ck("ingestion_jobs", "idempotency_key_length", "char_length(idempotency_key) BETWEEN 1 AND 200"),
        _ck("ingestion_jobs", "progress_object", "jsonb_typeof(progress) = 'object'"),
        sa.ForeignKeyConstraint(
            ["document_id"],
            ["documents.id"],
            name=op.f("fk_ingestion_jobs_document_id_documents"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_version_id"],
            ["document_versions.id"],
            name=op.f("fk_ingestion_jobs_document_version_id_document_versions"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"],
            ["users.id"],
            name=op.f("fk_ingestion_jobs_requested_by_users"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_ingestion_jobs")),
        sa.UniqueConstraint("idempotency_key", name=op.f("uq_ingestion_jobs_idempotency_key")),
    )
    op.create_index("ix_ingestion_jobs_status", "ingestion_jobs", ["status"])
    op.create_index("ix_ingestion_jobs_document_id", "ingestion_jobs", ["document_id"])
    op.create_index("ix_ingestion_jobs_queued_at_id", "ingestion_jobs", ["queued_at", "id"])


def downgrade() -> None:
    # Runtime HNSW indexes (ADR-017) live on ``embeddings`` and are dropped with the table.
    op.drop_table("ingestion_jobs")
    op.drop_table("embeddings")
    op.drop_table("document_chunks")
    op.drop_constraint(
        op.f("fk_documents_current_version_id_document_versions"), "documents", type_="foreignkey"
    )
    op.drop_table("document_versions")
    op.drop_table("documents")
    op.drop_table("knowledge_base_sources")
    op.drop_table("data_sources")
    op.drop_table("knowledge_base_access")
    op.drop_table("knowledge_bases")
