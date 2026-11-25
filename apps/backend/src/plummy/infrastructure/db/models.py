"""ORM models for system and identity tables (DATABASE_SCHEMA.md §3, §7.3).

ORM entities never leave the infrastructure layer: repositories map them to domain dataclasses.
Every change here needs an Alembic revision; a test compares the models with the migrated schema.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, CITEXT, INET, JSONB, TSVECTOR
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from plummy.infrastructure.db.base import Base
from plummy.infrastructure.db.types import Vector

NOW = text("now()")


def _ts(*, nullable: bool = True, default_now: bool = False) -> Mapped[Any]:
    return mapped_column(
        DateTime(timezone=True), nullable=nullable, server_default=NOW if default_now else None
    )


class UserModel(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("status IN ('active', 'disabled', 'invited')", name="status"),
        CheckConstraint("char_length(display_name) BETWEEN 1 AND 200", name="display_name_length"),
        Index("ix_users_status", "status"),
        # A soft-deleted user frees its email (ADR-015 / DATABASE_SCHEMA.md §3.2).
        Index("uq_users_email_active", "email", unique=True, postgresql_where=text("deleted_at IS NULL")),
        Index("ix_users_created_at_id", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    email: Mapped[str] = mapped_column(CITEXT(), nullable=False)
    display_name: Mapped[str] = mapped_column(Text(), nullable=False)
    password_hash: Mapped[str | None] = mapped_column(Text(), nullable=True)
    status: Mapped[str] = mapped_column(Text(), nullable=False, server_default=text("'active'"))
    last_login_at: Mapped[datetime | None] = _ts()
    password_changed_at: Mapped[datetime | None] = _ts()
    deleted_at: Mapped[datetime | None] = _ts()
    created_at: Mapped[datetime] = _ts(nullable=False, default_now=True)
    updated_at: Mapped[datetime] = _ts(nullable=False, default_now=True)


class InstanceStateModel(Base):
    __tablename__ = "instance_state"
    __table_args__ = (
        CheckConstraint("id = 1", name="singleton"),
        CheckConstraint(
            "initialized_at IS NULL OR initialized_by_user_id IS NOT NULL", name="initialized_by"
        ),
        CheckConstraint(
            "bootstrap_token_hash IS NULL OR octet_length(bootstrap_token_hash) = 32",
            name="bootstrap_token_hash_length",
        ),
    )

    id: Mapped[int] = mapped_column(SmallInteger(), primary_key=True, autoincrement=False)
    initialized_at: Mapped[datetime | None] = _ts()
    initialized_by_user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    bootstrap_token_hash: Mapped[bytes | None] = mapped_column(LargeBinary(), nullable=True)
    bootstrap_token_expires_at: Mapped[datetime | None] = _ts()
    bootstrap_token_used_at: Mapped[datetime | None] = _ts()
    updated_at: Mapped[datetime] = _ts(nullable=False, default_now=True)


class PermissionModel(Base):
    __tablename__ = "permissions"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    key: Mapped[str] = mapped_column(Text(), nullable=False, unique=True)
    description: Mapped[str] = mapped_column(Text(), nullable=False)
    created_at: Mapped[datetime] = _ts(nullable=False, default_now=True)


class RoleModel(Base):
    __tablename__ = "roles"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    key: Mapped[str] = mapped_column(Text(), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(Text(), nullable=False)
    description: Mapped[str] = mapped_column(Text(), nullable=False)
    is_system: Mapped[bool] = mapped_column(Boolean(), nullable=False, server_default=text("false"))
    created_at: Mapped[datetime] = _ts(nullable=False, default_now=True)
    updated_at: Mapped[datetime] = _ts(nullable=False, default_now=True)


class RolePermissionModel(Base):
    __tablename__ = "role_permissions"
    __table_args__ = (Index("ix_role_permissions_permission_id", "permission_id"),)

    role_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("roles.id", ondelete="CASCADE"), primary_key=True
    )
    permission_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("permissions.id", ondelete="CASCADE"), primary_key=True
    )


class UserRoleModel(Base):
    __tablename__ = "user_roles"
    __table_args__ = (Index("ix_user_roles_role_id", "role_id"),)

    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("roles.id", ondelete="RESTRICT"), primary_key=True
    )
    granted_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = _ts(nullable=False, default_now=True)


class GroupModel(Base):
    __tablename__ = "groups"
    __table_args__ = (
        CheckConstraint("char_length(name) BETWEEN 1 AND 100", name="name_length"),
        CheckConstraint("description IS NULL OR char_length(description) <= 500", name="description_length"),
        Index("uq_groups_name_active", "name", unique=True, postgresql_where=text("deleted_at IS NULL")),
        Index("ix_groups_created_at_id", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(CITEXT(), nullable=False)
    description: Mapped[str | None] = mapped_column(Text(), nullable=True)
    created_at: Mapped[datetime] = _ts(nullable=False, default_now=True)
    updated_at: Mapped[datetime] = _ts(nullable=False, default_now=True)
    deleted_at: Mapped[datetime | None] = _ts()


class UserGroupModel(Base):
    __tablename__ = "user_groups"
    __table_args__ = (Index("ix_user_groups_group_id", "group_id"),)

    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    group_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = _ts(nullable=False, default_now=True)


class GroupRoleModel(Base):
    __tablename__ = "group_roles"
    __table_args__ = (Index("ix_group_roles_role_id", "role_id"),)

    group_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("groups.id", ondelete="CASCADE"), primary_key=True
    )
    role_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("roles.id", ondelete="RESTRICT"), primary_key=True
    )
    granted_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = _ts(nullable=False, default_now=True)


class ActivationTokenModel(Base):
    __tablename__ = "user_activation_tokens"
    __table_args__ = (CheckConstraint("octet_length(token_hash) = 32", name="token_hash_length"),)

    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(), nullable=False, unique=True)
    expires_at: Mapped[datetime] = _ts(nullable=False)
    created_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = _ts(nullable=False, default_now=True)


class SessionModel(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint(
            "revoke_reason IN ('logout', 'superseded', 'admin', 'password_change', 'user_disabled')",
            name="revoke_reason",
        ),
        CheckConstraint("(revoked_at IS NULL) = (revoke_reason IS NULL)", name="revoked_consistency"),
        CheckConstraint("octet_length(token_hash) = 32", name="token_hash_length"),
        CheckConstraint("char_length(user_agent) <= 512", name="user_agent_length"),
        Index("ix_sessions_user_id_active", "user_id", postgresql_where=text("revoked_at IS NULL")),
        Index("ix_sessions_expires_at", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash: Mapped[bytes] = mapped_column(LargeBinary(), nullable=False, unique=True)
    created_at: Mapped[datetime] = _ts(nullable=False, default_now=True)
    last_seen_at: Mapped[datetime] = _ts(nullable=False)
    expires_at: Mapped[datetime] = _ts(nullable=False)
    revoked_at: Mapped[datetime | None] = _ts()
    revoke_reason: Mapped[str | None] = mapped_column(Text(), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text(), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(INET(), nullable=True)


class AuditLogModel(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        CheckConstraint("actor_type IN ('user', 'anonymous', 'system', 'worker')", name="actor_type"),
        CheckConstraint("outcome IN ('success', 'denied', 'failure')", name="outcome"),
        Index("ix_audit_logs_created_at", text("created_at DESC")),
        Index("ix_audit_logs_action_created_at", "action", "created_at"),
        Index("ix_audit_logs_target", "target_type", "target_id"),
        Index("ix_audit_logs_actor_user_id_created_at", "actor_user_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    created_at: Mapped[datetime] = _ts(nullable=False, default_now=True)
    actor_type: Mapped[str] = mapped_column(Text(), nullable=False)
    # No foreign key: audit records outlive the users they mention.
    actor_user_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    action: Mapped[str] = mapped_column(Text(), nullable=False)
    target_type: Mapped[str | None] = mapped_column(Text(), nullable=True)
    target_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    outcome: Mapped[str] = mapped_column(Text(), nullable=False)
    correlation_id: Mapped[str | None] = mapped_column(Text(), nullable=True)
    ip_address: Mapped[str | None] = mapped_column(INET(), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(Text(), nullable=True)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB(), nullable=False, server_default=text("'{}'::jsonb")
    )


class SecretReferenceModel(Base):
    """Encrypted secrets (ADR-016). Only ciphertext: the key ring lives outside the database."""

    __tablename__ = "secret_references"
    __table_args__ = (
        CheckConstraint("kind IN ('ai_api_key', 'oauth_refresh_token', 'oauth_client_secret')", name="kind"),
        CheckConstraint("backend = 'db_encrypted'", name="backend"),
        CheckConstraint("algorithm = 'AES-256-GCM'", name="algorithm"),
        CheckConstraint("octet_length(nonce) = 12", name="nonce_length"),
        # GCM tag (16 bytes) plus at least one byte of plaintext.
        CheckConstraint("octet_length(ciphertext) > 16", name="ciphertext_length"),
        CheckConstraint("key_id ~ '^[A-Za-z0-9._-]{1,32}$'", name="key_id_format"),
        CheckConstraint("hint IS NULL OR char_length(hint) = 4", name="hint_length"),
        Index("ix_secret_references_key_id", "key_id"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    kind: Mapped[str] = mapped_column(Text(), nullable=False)
    backend: Mapped[str] = mapped_column(Text(), nullable=False, server_default=text("'db_encrypted'"))
    ciphertext: Mapped[bytes] = mapped_column(LargeBinary(), nullable=False)
    nonce: Mapped[bytes] = mapped_column(LargeBinary(), nullable=False)
    key_id: Mapped[str] = mapped_column(Text(), nullable=False)
    algorithm: Mapped[str] = mapped_column(Text(), nullable=False)
    hint: Mapped[str | None] = mapped_column(Text(), nullable=True)
    created_at: Mapped[datetime] = _ts(nullable=False, default_now=True)
    updated_at: Mapped[datetime] = _ts(nullable=False, default_now=True)
    rotated_at: Mapped[datetime | None] = _ts()


class AIProviderModel(Base):
    """AI providers (DATABASE_SCHEMA.md §4.2). The API key is only referenced (``secret_references``)."""

    __tablename__ = "ai_providers"
    __table_args__ = (
        CheckConstraint(
            "provider_type IN ('openai', 'anthropic', 'ollama', 'openai_compatible')", name="provider_type"
        ),
        CheckConstraint("char_length(name) BETWEEN 1 AND 100", name="name_length"),
        CheckConstraint("char_length(base_url) BETWEEN 1 AND 2048", name="base_url_length"),
        CheckConstraint(
            "last_test_status IS NULL OR last_test_status IN ('ok', 'failed')", name="last_test_status"
        ),
        # Only a typed error code is stored, never provider output.
        CheckConstraint(
            "last_test_error IS NULL OR last_test_error ~ '^[a-z_]{1,64}$'", name="last_test_error_code"
        ),
        CheckConstraint("jsonb_typeof(settings) = 'object'", name="settings_object"),
        Index(
            "uq_ai_providers_name_active", "name", unique=True, postgresql_where=text("deleted_at IS NULL")
        ),
        Index("ix_ai_providers_created_at_id", "created_at", "id"),
        Index("uq_ai_providers_secret_ref_id", "secret_ref_id", unique=True),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(CITEXT(), nullable=False)
    provider_type: Mapped[str] = mapped_column(Text(), nullable=False)
    base_url: Mapped[str] = mapped_column(Text(), nullable=False)
    secret_ref_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("secret_references.id", ondelete="SET NULL"), nullable=True
    )
    settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB(), nullable=False, server_default=text("'{}'::jsonb")
    )
    enabled: Mapped[bool] = mapped_column(Boolean(), nullable=False, server_default=text("true"))
    last_test_at: Mapped[datetime | None] = _ts()
    last_test_status: Mapped[str | None] = mapped_column(Text(), nullable=True)
    last_test_error: Mapped[str | None] = mapped_column(Text(), nullable=True)
    created_at: Mapped[datetime] = _ts(nullable=False, default_now=True)
    updated_at: Mapped[datetime] = _ts(nullable=False, default_now=True)
    deleted_at: Mapped[datetime | None] = _ts()


class ModelConfigModel(Base):
    """Chat models of a provider (DATABASE_SCHEMA.md §4.3)."""

    __tablename__ = "model_configs"
    __table_args__ = (
        CheckConstraint("kind = 'chat'", name="kind"),
        CheckConstraint("char_length(model_id) BETWEEN 1 AND 256", name="model_id_length"),
        CheckConstraint(
            "display_name IS NULL OR char_length(display_name) <= 200", name="display_name_length"
        ),
        CheckConstraint("context_window IS NULL OR context_window > 0", name="context_window"),
        CheckConstraint("max_output_tokens IS NULL OR max_output_tokens > 0", name="max_output_tokens"),
        UniqueConstraint("provider_id", "model_id"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    provider_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("ai_providers.id", ondelete="CASCADE"), nullable=False
    )
    model_id: Mapped[str] = mapped_column(Text(), nullable=False)
    display_name: Mapped[str | None] = mapped_column(Text(), nullable=True)
    kind: Mapped[str] = mapped_column(Text(), nullable=False, server_default=text("'chat'"))
    context_window: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    max_output_tokens: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    default_params: Mapped[dict[str, Any]] = mapped_column(
        JSONB(), nullable=False, server_default=text("'{}'::jsonb")
    )
    capabilities: Mapped[dict[str, Any]] = mapped_column(
        JSONB(), nullable=False, server_default=text("'{}'::jsonb")
    )
    enabled: Mapped[bool] = mapped_column(Boolean(), nullable=False, server_default=text("true"))
    created_at: Mapped[datetime] = _ts(nullable=False, default_now=True)
    updated_at: Mapped[datetime] = _ts(nullable=False, default_now=True)


class EmbeddingProfileModel(Base):
    """Embedding profiles (DATABASE_SCHEMA.md §4.4). Immutable except ``name`` and ``status``."""

    __tablename__ = "embedding_provider_configs"
    __table_args__ = (
        CheckConstraint("char_length(name) BETWEEN 1 AND 100", name="name_length"),
        CheckConstraint("char_length(model_id) BETWEEN 1 AND 256", name="model_id_length"),
        CheckConstraint("dimensions BETWEEN 1 AND 16000", name="dimensions"),
        CheckConstraint("distance_metric IN ('cosine', 'inner_product', 'l2')", name="distance_metric"),
        CheckConstraint("max_input_tokens IS NULL OR max_input_tokens > 0", name="max_input_tokens"),
        CheckConstraint("status IN ('active', 'deprecated', 'retired')", name="status"),
        UniqueConstraint("name"),
        Index("ix_embedding_provider_configs_provider_id", "provider_id"),
        Index("ix_embedding_provider_configs_created_at_id", "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(CITEXT(), nullable=False)
    provider_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("ai_providers.id", ondelete="RESTRICT"), nullable=False
    )
    model_id: Mapped[str] = mapped_column(Text(), nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer(), nullable=False)
    distance_metric: Mapped[str] = mapped_column(Text(), nullable=False, server_default=text("'cosine'"))
    normalize: Mapped[bool] = mapped_column(Boolean(), nullable=False, server_default=text("false"))
    max_input_tokens: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    status: Mapped[str] = mapped_column(Text(), nullable=False, server_default=text("'active'"))
    created_at: Mapped[datetime] = _ts(nullable=False, default_now=True)
    updated_at: Mapped[datetime] = _ts(nullable=False, default_now=True)


# --- knowledge (DATABASE_SCHEMA.md §5) ---------------------------------------------------------------------

_KEY_CHECK = "~ '^[0-9a-f]{32}$'"  # server-generated storage keys (application/storage.py)
_ERROR_CODE_CHECK = "~ '^[a-z_]{1,64}$'"  # typed codes only, never document content or provider output
# Full-text search configuration of chunks (ADR-018); retrieval parses queries with the same configuration.
FTS_CONFIG = "russian"
SEARCH_VECTOR_EXPRESSION = f"to_tsvector('{FTS_CONFIG}'::regconfig, content)"


class KnowledgeBaseModel(Base):
    """Knowledge bases (DATABASE_SCHEMA.md §5.1). Soft-deleted; the name is unique among live ones."""

    __tablename__ = "knowledge_bases"
    __table_args__ = (
        CheckConstraint("char_length(name) BETWEEN 1 AND 200", name="name_length"),
        CheckConstraint("description IS NULL OR char_length(description) <= 2000", name="description_length"),
        CheckConstraint("jsonb_typeof(chunking_settings) = 'object'", name="chunking_settings_object"),
        Index(
            "uq_knowledge_bases_name_active", "name", unique=True, postgresql_where=text("deleted_at IS NULL")
        ),
        Index("ix_knowledge_bases_created_at_id", "created_at", "id"),
        Index("ix_knowledge_bases_embedding_config_id", "embedding_config_id"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(CITEXT(), nullable=False)
    description: Mapped[str | None] = mapped_column(Text(), nullable=True)
    embedding_config_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("embedding_provider_configs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    chunking_settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB(), nullable=False, server_default=text("'{}'::jsonb")
    )
    created_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = _ts(nullable=False, default_now=True)
    updated_at: Mapped[datetime] = _ts(nullable=False, default_now=True)
    deleted_at: Mapped[datetime | None] = _ts()


class KnowledgeBaseAccessModel(Base):
    """KB-level grants to a user or a group (DATABASE_SCHEMA.md §5.2)."""

    __tablename__ = "knowledge_base_access"
    __table_args__ = (
        CheckConstraint("principal_type IN ('user', 'group')", name="principal_type"),
        CheckConstraint("access_level IN ('read', 'manage')", name="access_level"),
        CheckConstraint(
            "(principal_type = 'user') = (user_id IS NOT NULL) "
            "AND (principal_type = 'group') = (group_id IS NOT NULL)",
            name="principal",
        ),
        UniqueConstraint("knowledge_base_id", "user_id"),
        UniqueConstraint("knowledge_base_id", "group_id"),
        Index("ix_knowledge_base_access_user_id", "user_id"),
        Index("ix_knowledge_base_access_group_id", "group_id"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    knowledge_base_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), nullable=False
    )
    principal_type: Mapped[str] = mapped_column(Text(), nullable=False)
    user_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True
    )
    group_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("groups.id", ondelete="CASCADE"), nullable=True
    )
    access_level: Mapped[str] = mapped_column(Text(), nullable=False)
    granted_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = _ts(nullable=False, default_now=True)


class DataSourceModel(Base):
    """Document sources (DATABASE_SCHEMA.md §5.4, ADR-008). Sync columns arrive with the connectors."""

    __tablename__ = "data_sources"
    __table_args__ = (
        CheckConstraint("type IN ('upload', 'google_drive', 'microsoft_365')", name="type"),
        CheckConstraint("status IN ('active', 'paused', 'error', 'disconnected')", name="status"),
        CheckConstraint("char_length(name) BETWEEN 1 AND 200", name="name_length"),
        CheckConstraint("jsonb_typeof(config) = 'object'", name="config_object"),
        CheckConstraint("(type = 'upload') = (owner_knowledge_base_id IS NOT NULL)", name="upload_owner"),
        CheckConstraint(
            "sync_interval_minutes IS NULL OR sync_interval_minutes BETWEEN 5 AND 1440", name="sync_interval"
        ),
        CheckConstraint(
            "last_sync_status IS NULL OR last_sync_status IN ('succeeded', 'failed', 'cancelled')",
            name="last_sync_status",
        ),
        CheckConstraint("sync_cursor IS NULL OR char_length(sync_cursor) <= 2048", name="cursor_length"),
        CheckConstraint(f"last_error IS NULL OR last_error {_ERROR_CODE_CHECK}", name="last_error_format"),
        UniqueConstraint("owner_knowledge_base_id"),
        Index(
            "ix_data_sources_next_sync_at",
            "next_sync_at",
            postgresql_where=text("status = 'active' AND deleted_at IS NULL AND next_sync_at IS NOT NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    type: Mapped[str] = mapped_column(Text(), nullable=False)
    owner_knowledge_base_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("knowledge_bases.id", ondelete="RESTRICT"), nullable=True
    )
    name: Mapped[str] = mapped_column(Text(), nullable=False)
    status: Mapped[str] = mapped_column(Text(), nullable=False, server_default=text("'active'"))
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB(), nullable=False, server_default=text("'{}'::jsonb")
    )
    # Sync state of external sources (ADR-020 §8); the cursor moves only after a processed page.
    sync_cursor: Mapped[str | None] = mapped_column(Text(), nullable=True)
    sync_interval_minutes: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    next_sync_at: Mapped[datetime | None] = _ts()
    last_sync_at: Mapped[datetime | None] = _ts()
    last_reconciled_at: Mapped[datetime | None] = _ts()
    last_sync_status: Mapped[str | None] = mapped_column(Text(), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text(), nullable=True)
    created_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = _ts(nullable=False, default_now=True)
    updated_at: Mapped[datetime] = _ts(nullable=False, default_now=True)
    deleted_at: Mapped[datetime | None] = _ts()


class DataSourceCredentialModel(Base):
    """OAuth credentials of an external source (DATABASE_SCHEMA.md §5.5, ADR-020 §7): a reference to the
    encrypted refresh token only; the access token is never stored."""

    __tablename__ = "data_source_credentials"
    __table_args__ = (
        CheckConstraint("status IN ('valid', 'expired', 'revoked')", name="status"),
        CheckConstraint(
            "account_label IS NULL OR char_length(account_label) <= 320", name="account_label_length"
        ),
        CheckConstraint("(status = 'revoked') = (revoked_at IS NOT NULL)", name="revoked_consistent"),
        UniqueConstraint("secret_ref_id"),
    )

    data_source_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("data_sources.id", ondelete="CASCADE"), primary_key=True
    )
    secret_ref_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("secret_references.id", ondelete="RESTRICT"), nullable=False
    )
    account_label: Mapped[str | None] = mapped_column(Text(), nullable=True)
    granted_scopes: Mapped[list[str]] = mapped_column(ARRAY(Text()), nullable=False)
    status: Mapped[str] = mapped_column(Text(), nullable=False)
    created_at: Mapped[datetime] = _ts(nullable=False, default_now=True)
    updated_at: Mapped[datetime] = _ts(nullable=False, default_now=True)
    last_refreshed_at: Mapped[datetime | None] = _ts()
    revoked_at: Mapped[datetime | None] = _ts()
    connected_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class SyncJobModel(Base):
    """Sync runs of an external source (DATABASE_SCHEMA.md §7.2, ADR-020 §8); one active per source."""

    __tablename__ = "sync_jobs"
    __table_args__ = (
        CheckConstraint("trigger IN ('manual', 'schedule', 'reconciliation', 'initial')", name="trigger"),
        CheckConstraint("mode IN ('initial', 'incremental', 'reconciliation')", name="mode"),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'dead')", name="status"
        ),
        CheckConstraint("jsonb_typeof(stats) = 'object'", name="stats_object"),
        CheckConstraint("attempt >= 0 AND max_attempts BETWEEN 1 AND 20", name="attempts"),
        CheckConstraint(f"error_code IS NULL OR error_code {_ERROR_CODE_CHECK}", name="error_code_format"),
        CheckConstraint(
            "error_message IS NULL OR char_length(error_message) <= 500", name="error_message_length"
        ),
        Index(
            "sync_jobs_one_active_per_source",
            "data_source_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
        Index(
            "ix_sync_jobs_data_source_id_queued_at", "data_source_id", text("queued_at DESC"), text("id DESC")
        ),
        Index(
            "ix_sync_jobs_status_updated_at",
            "status",
            "updated_at",
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    data_source_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("data_sources.id", ondelete="CASCADE"), nullable=False
    )
    trigger: Mapped[str] = mapped_column(Text(), nullable=False)
    mode: Mapped[str] = mapped_column(Text(), nullable=False)
    status: Mapped[str] = mapped_column(Text(), nullable=False)
    cursor_before: Mapped[str | None] = mapped_column(Text(), nullable=True)
    cursor_after: Mapped[str | None] = mapped_column(Text(), nullable=True)
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB(), nullable=False, server_default=text("'{}'::jsonb"))
    attempt: Mapped[int] = mapped_column(Integer(), nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer(), nullable=False, server_default=text("5"))
    celery_task_id: Mapped[str | None] = mapped_column(Text(), nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text(), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text(), nullable=True)
    requested_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    queued_at: Mapped[datetime] = _ts(nullable=False, default_now=True)
    started_at: Mapped[datetime | None] = _ts()
    finished_at: Mapped[datetime | None] = _ts()
    updated_at: Mapped[datetime] = _ts(nullable=False, default_now=True)


class KnowledgeBaseSourceModel(Base):
    """Sources attached to a knowledge base (DATABASE_SCHEMA.md §5.6)."""

    __tablename__ = "knowledge_base_sources"
    # A source belongs to one knowledge base (ADR-020 §6).
    __table_args__ = (UniqueConstraint("data_source_id"),)

    knowledge_base_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), primary_key=True
    )
    data_source_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("data_sources.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = _ts(nullable=False, default_now=True)


class AssistantModel(Base):
    """Assistants (DATABASE_SCHEMA.md §5.3, ADR-005). Soft-deleted; the name is unique among live ones."""

    __tablename__ = "assistants"
    __table_args__ = (
        CheckConstraint("char_length(name) BETWEEN 1 AND 200", name="name_length"),
        CheckConstraint("description IS NULL OR char_length(description) <= 2000", name="description_length"),
        CheckConstraint("char_length(instructions) <= 20000", name="instructions_length"),
        CheckConstraint("jsonb_typeof(retrieval_settings) = 'object'", name="retrieval_settings_object"),
        CheckConstraint("max_context_tokens BETWEEN 256 AND 128000", name="max_context_tokens"),
        CheckConstraint("max_history_messages BETWEEN 0 AND 100", name="max_history_messages"),
        CheckConstraint("max_output_tokens BETWEEN 1 AND 128000", name="max_output_tokens"),
        CheckConstraint("status IN ('active', 'disabled')", name="status"),
        CheckConstraint("model_config_id IS NOT NULL OR deleted_at IS NOT NULL", name="model_config"),
        Index("uq_assistants_name_active", "name", unique=True, postgresql_where=text("deleted_at IS NULL")),
        Index("ix_assistants_created_at_id", "created_at", "id"),
        Index("ix_assistants_model_config_id", "model_config_id"),
        Index("ix_assistants_embedding_config_id", "embedding_config_id"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    name: Mapped[str] = mapped_column(CITEXT(), nullable=False)
    description: Mapped[str | None] = mapped_column(Text(), nullable=True)
    instructions: Mapped[str] = mapped_column(Text(), nullable=False, server_default=text("''"))
    model_config_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("model_configs.id", ondelete="SET NULL"), nullable=True
    )
    embedding_config_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("embedding_provider_configs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    retrieval_settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB(), nullable=False, server_default=text("'{}'::jsonb")
    )
    max_context_tokens: Mapped[int] = mapped_column(Integer(), nullable=False, server_default=text("8000"))
    max_history_messages: Mapped[int] = mapped_column(Integer(), nullable=False, server_default=text("10"))
    max_output_tokens: Mapped[int] = mapped_column(Integer(), nullable=False, server_default=text("1024"))
    status: Mapped[str] = mapped_column(Text(), nullable=False, server_default=text("'active'"))
    created_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = _ts(nullable=False, default_now=True)
    updated_at: Mapped[datetime] = _ts(nullable=False, default_now=True)
    deleted_at: Mapped[datetime | None] = _ts()


class AssistantKnowledgeBaseModel(Base):
    """Knowledge bases of an assistant (DATABASE_SCHEMA.md §5.3)."""

    __tablename__ = "assistant_knowledge_bases"
    __table_args__ = (Index("ix_assistant_knowledge_bases_knowledge_base_id", "knowledge_base_id"),)

    assistant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("assistants.id", ondelete="CASCADE"), primary_key=True
    )
    knowledge_base_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("knowledge_bases.id", ondelete="CASCADE"), primary_key=True
    )
    created_at: Mapped[datetime] = _ts(nullable=False, default_now=True)


class DocumentModel(Base):
    """Documents (DATABASE_SCHEMA.md §5.7). ``filename`` is for display only, never a storage path."""

    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint("char_length(external_id) BETWEEN 1 AND 512", name="external_id_length"),
        CheckConstraint("char_length(filename) BETWEEN 1 AND 255", name="filename_length"),
        CheckConstraint(
            "processing_status IN ('pending', 'processing', 'indexed', 'failed', 'skipped')",
            name="processing_status",
        ),
        CheckConstraint(
            "deletion_reason IS NULL OR deletion_reason IN "
            "('user', 'remote_deleted', 'out_of_scope', 'source_deleted', 'kb_deleted')",
            name="deletion_reason",
        ),
        CheckConstraint("(deleted_at IS NULL) = (deletion_reason IS NULL)", name="deletion_consistent"),
        CheckConstraint("jsonb_typeof(source_metadata) = 'object'", name="source_metadata_object"),
        UniqueConstraint("data_source_id", "external_id"),
        Index("ix_documents_data_source_id_created_at_id", "data_source_id", "created_at", "id"),
        Index("ix_documents_processing_status", "processing_status"),
        Index("ix_documents_deleted_at", "deleted_at"),
        Index("ix_documents_updated_at", "updated_at"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    data_source_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("data_sources.id", ondelete="RESTRICT"), nullable=False
    )
    external_id: Mapped[str] = mapped_column(Text(), nullable=False)
    filename: Mapped[str] = mapped_column(Text(), nullable=False)
    mime_type: Mapped[str | None] = mapped_column(Text(), nullable=True)
    external_path: Mapped[str | None] = mapped_column(Text(), nullable=True)
    external_etag: Mapped[str | None] = mapped_column(Text(), nullable=True)
    current_version_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "document_versions.id",
            ondelete="SET NULL",
            deferrable=True,
            initially="DEFERRED",
            use_alter=True,
        ),
        nullable=True,
    )
    processing_status: Mapped[str] = mapped_column(Text(), nullable=False, server_default=text("'pending'"))
    source_created_at: Mapped[datetime | None] = _ts()
    source_modified_at: Mapped[datetime | None] = _ts()
    source_metadata: Mapped[dict[str, Any]] = mapped_column(
        JSONB(), nullable=False, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = _ts(nullable=False, default_now=True)
    updated_at: Mapped[datetime] = _ts(nullable=False, default_now=True)
    deleted_at: Mapped[datetime | None] = _ts()
    deletion_reason: Mapped[str | None] = mapped_column(Text(), nullable=True)
    purged_at: Mapped[datetime | None] = _ts()


class DocumentVersionModel(Base):
    """Immutable document versions (DATABASE_SCHEMA.md §5.8): content never changes, only processing state."""

    __tablename__ = "document_versions"
    __table_args__ = (
        CheckConstraint("version_number > 0", name="version_number"),
        CheckConstraint("octet_length(checksum_sha256) = 32", name="checksum_length"),
        CheckConstraint("size_bytes >= 0", name="size_bytes"),
        CheckConstraint("page_count IS NULL OR page_count >= 0", name="page_count"),
        CheckConstraint(f"storage_key {_KEY_CHECK}", name="storage_key_format"),
        CheckConstraint(
            f"extracted_storage_key IS NULL OR extracted_storage_key {_KEY_CHECK}",
            name="extracted_storage_key_format",
        ),
        CheckConstraint(
            "status IN ('pending', 'processing', 'ready', 'failed', 'outdated', 'deleted')", name="status"
        ),
        CheckConstraint(f"error_code IS NULL OR error_code {_ERROR_CODE_CHECK}", name="error_code_format"),
        CheckConstraint(
            "error_message IS NULL OR char_length(error_message) <= 500", name="error_message_length"
        ),
        CheckConstraint("active_chunk_set IS NULL OR active_chunk_set > 0", name="active_chunk_set"),
        CheckConstraint("jsonb_typeof(metadata) = 'object'", name="metadata_object"),
        UniqueConstraint("document_id", "version_number"),
        UniqueConstraint("document_id", "checksum_sha256"),
        Index("ix_document_versions_status", "status"),
        Index("ix_document_versions_document_id_status", "document_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    document_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer(), nullable=False)
    checksum_sha256: Mapped[bytes] = mapped_column(LargeBinary(), nullable=False)
    mime_type: Mapped[str] = mapped_column(Text(), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    page_count: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    storage_key: Mapped[str] = mapped_column(Text(), nullable=False)
    extracted_storage_key: Mapped[str | None] = mapped_column(Text(), nullable=True)
    external_version: Mapped[str | None] = mapped_column(Text(), nullable=True)
    parser: Mapped[str | None] = mapped_column(Text(), nullable=True)
    status: Mapped[str] = mapped_column(Text(), nullable=False, server_default=text("'pending'"))
    error_code: Mapped[str | None] = mapped_column(Text(), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text(), nullable=True)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONB(), nullable=False, server_default=text("'{}'::jsonb")
    )
    active_chunk_set: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    processed_at: Mapped[datetime | None] = _ts()
    created_at: Mapped[datetime] = _ts(nullable=False, default_now=True)
    updated_at: Mapped[datetime] = _ts(nullable=False, default_now=True)


class DocumentChunkModel(Base):
    """Chunks of a version (DATABASE_SCHEMA.md §5.9); PostgreSQL computes ``search_vector`` (ADR-018)."""

    __tablename__ = "document_chunks"
    __table_args__ = (
        CheckConstraint("chunk_set > 0", name="chunk_set"),
        CheckConstraint("chunk_index >= 0", name="chunk_index"),
        CheckConstraint("octet_length(content_hash) = 32", name="content_hash_length"),
        CheckConstraint("token_count >= 0", name="token_count"),
        CheckConstraint(
            "page_start IS NULL OR page_end IS NULL OR page_start <= page_end", name="page_range"
        ),
        CheckConstraint("row_start IS NULL OR row_end IS NULL OR row_start <= row_end", name="row_range"),
        UniqueConstraint("document_version_id", "chunk_set", "chunk_index"),
        Index("ix_document_chunks_data_source_id", "data_source_id"),
        Index("ix_document_chunks_document_id", "document_id"),
        Index("ix_document_chunks_search_vector", "search_vector", postgresql_using="gin"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    document_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    document_version_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("document_versions.id", ondelete="CASCADE"), nullable=False
    )
    data_source_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    chunk_set: Mapped[int] = mapped_column(Integer(), nullable=False)
    chunk_index: Mapped[int] = mapped_column(Integer(), nullable=False)
    content: Mapped[str] = mapped_column(Text(), nullable=False)
    content_hash: Mapped[bytes] = mapped_column(LargeBinary(), nullable=False)
    token_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    page_start: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    page_end: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    section_path: Mapped[list[str] | None] = mapped_column(ARRAY(Text()), nullable=True)
    sheet_name: Mapped[str | None] = mapped_column(Text(), nullable=True)
    row_start: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    row_end: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    # Never written by the application: the database derives it from ``content`` (ADR-018).
    search_vector: Mapped[str] = mapped_column(
        TSVECTOR(), Computed(SEARCH_VECTOR_EXPRESSION, persisted=True), nullable=False
    )
    created_at: Mapped[datetime] = _ts(nullable=False, default_now=True)


class EmbeddingModel(Base):
    """Vectors of chunks per embedding profile (ADR-017 §1); per-profile HNSW indexes are made at runtime."""

    __tablename__ = "embeddings"
    __table_args__ = (
        CheckConstraint("dimensions BETWEEN 1 AND 4000", name="dimensions"),
        CheckConstraint("vector_dims(embedding) = dimensions", name="embedding_dimensions"),
        UniqueConstraint("chunk_id", "embedding_config_id"),
        Index("ix_embeddings_config_version", "embedding_config_id", "document_version_id"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    chunk_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("document_chunks.id", ondelete="CASCADE"), nullable=False
    )
    embedding_config_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("embedding_provider_configs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    document_version_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    data_source_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    dimensions: Mapped[int] = mapped_column(Integer(), nullable=False)
    embedding: Mapped[Any] = mapped_column(Vector(), nullable=False)
    created_at: Mapped[datetime] = _ts(nullable=False, default_now=True)


class IngestionJobModel(Base):
    """Ingestion attempts (DATABASE_SCHEMA.md §7.1); ``idempotency_key`` makes repeats a no-op (SC-09)."""

    __tablename__ = "ingestion_jobs"
    __table_args__ = (
        CheckConstraint("kind IN ('ingest', 'reindex', 'delete', 'purge')", name="kind"),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled', 'dead')", name="status"
        ),
        CheckConstraint(
            "stage IN ('fetch', 'authorize', 'validate_type', 'security_check', 'store_original', 'parse', "
            "'normalize', 'chunk', 'embed', 'persist', 'finalize', 'report')",
            name="stage",
        ),
        CheckConstraint(
            "result IS NULL OR result IN ('processed', 'skipped_already_processed', 'skipped_unchanged')",
            name="result",
        ),
        CheckConstraint("attempt >= 0 AND max_attempts >= 1", name="attempts"),
        CheckConstraint(f"error_code IS NULL OR error_code {_ERROR_CODE_CHECK}", name="error_code_format"),
        CheckConstraint(
            "error_message IS NULL OR char_length(error_message) <= 500", name="error_message_length"
        ),
        CheckConstraint("char_length(idempotency_key) BETWEEN 1 AND 200", name="idempotency_key_length"),
        CheckConstraint("jsonb_typeof(progress) = 'object'", name="progress_object"),
        UniqueConstraint("idempotency_key"),
        Index("ix_ingestion_jobs_status", "status"),
        Index("ix_ingestion_jobs_document_id", "document_id"),
        Index("ix_ingestion_jobs_queued_at_id", "queued_at", "id"),
        Index(
            "ix_ingestion_jobs_unpublished",
            "updated_at",
            postgresql_where=text("status = 'queued' AND celery_task_id IS NULL"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    document_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False
    )
    document_version_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("document_versions.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(Text(), nullable=False)
    status: Mapped[str] = mapped_column(Text(), nullable=False, server_default=text("'queued'"))
    stage: Mapped[str] = mapped_column(Text(), nullable=False, server_default=text("'fetch'"))
    idempotency_key: Mapped[str] = mapped_column(Text(), nullable=False)
    result: Mapped[str | None] = mapped_column(Text(), nullable=True)
    attempt: Mapped[int] = mapped_column(Integer(), nullable=False, server_default=text("0"))
    max_attempts: Mapped[int] = mapped_column(Integer(), nullable=False, server_default=text("5"))
    celery_task_id: Mapped[str | None] = mapped_column(Text(), nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text(), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text(), nullable=True)
    progress: Mapped[dict[str, Any]] = mapped_column(
        JSONB(), nullable=False, server_default=text("'{}'::jsonb")
    )
    requested_by: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    queued_at: Mapped[datetime] = _ts(nullable=False, default_now=True)
    started_at: Mapped[datetime | None] = _ts()
    finished_at: Mapped[datetime | None] = _ts()
    # Last change of the row (set by the repository): the beat sweep finds queued jobs left unpublished.
    updated_at: Mapped[datetime] = _ts(nullable=False, default_now=True)


class ConversationModel(Base):
    """A user's conversation with an assistant (DATABASE_SCHEMA.md §6.1); private to its owner."""

    __tablename__ = "conversations"
    __table_args__ = (
        CheckConstraint("char_length(title) BETWEEN 1 AND 200", name="title_length"),
        Index(
            "ix_conversations_user_id_last_message_at",
            "user_id",
            text("last_message_at DESC"),
            text("id DESC"),
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index("ix_conversations_assistant_id", "assistant_id"),
        Index("ix_conversations_deleted_at", "deleted_at", postgresql_where=text("deleted_at IS NOT NULL")),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    assistant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("assistants.id", ondelete="RESTRICT"), nullable=False
    )
    title: Mapped[str] = mapped_column(Text(), nullable=False)
    created_at: Mapped[datetime] = _ts(nullable=False, default_now=True)
    updated_at: Mapped[datetime] = _ts(nullable=False, default_now=True)
    last_message_at: Mapped[datetime] = _ts(nullable=False, default_now=True)
    deleted_at: Mapped[datetime | None] = _ts()


class MessageModel(Base):
    """A message of a conversation (DATABASE_SCHEMA.md §6.2); ``client_message_id`` dedupes sending."""

    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint("sequence >= 1", name="sequence"),
        CheckConstraint("role IN ('user', 'assistant')", name="role"),
        CheckConstraint("status IN ('completed', 'streaming', 'stopped', 'failed')", name="status"),
        CheckConstraint("char_length(content) <= 200000", name="content_length"),
        CheckConstraint("(role = 'user') = (client_message_id IS NOT NULL)", name="client_message_id"),
        UniqueConstraint("conversation_id", "sequence"),
        UniqueConstraint("conversation_id", "client_message_id"),
        Index("ix_messages_streaming", "created_at", postgresql_where=text("status = 'streaming'")),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    conversation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer(), nullable=False)
    role: Mapped[str] = mapped_column(Text(), nullable=False)
    content: Mapped[str] = mapped_column(Text(), nullable=False, server_default=text("''"))
    status: Mapped[str] = mapped_column(Text(), nullable=False)
    client_message_id: Mapped[UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text(), nullable=True)
    created_at: Mapped[datetime] = _ts(nullable=False, default_now=True)
    completed_at: Mapped[datetime | None] = _ts()


class MessageCitationModel(Base):
    """A validated citation of an answer, as a snapshot (DATABASE_SCHEMA.md §6.3). No foreign keys to the
    document: history stays readable after a purge, and opening a citation checks access again."""

    __tablename__ = "message_citations"
    __table_args__ = (
        CheckConstraint("citation_key ~ '^c[0-9]{1,4}$'", name="citation_key"),
        CheckConstraint("ordinal >= 1", name="ordinal"),
        UniqueConstraint("message_id", "ordinal"),
    )

    message_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE"), primary_key=True
    )
    citation_key: Mapped[str] = mapped_column(Text(), primary_key=True)
    ordinal: Mapped[int] = mapped_column(Integer(), nullable=False)
    document_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    document_version_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    chunk_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    filename: Mapped[str] = mapped_column(Text(), nullable=False)
    page: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    section: Mapped[str | None] = mapped_column(Text(), nullable=True)
    sheet: Mapped[str | None] = mapped_column(Text(), nullable=True)
    row_index: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    snippet: Mapped[str] = mapped_column(Text(), nullable=False)
    viewer_route: Mapped[str] = mapped_column(Text(), nullable=False)


class GenerationRunModel(Base):
    """One answer generation: configuration used, outcome and metrics (DATABASE_SCHEMA.md §6.4, ADR-019).
    No prompt and no retrieved text: only the chunk IDs."""

    __tablename__ = "generation_runs"
    __table_args__ = (
        CheckConstraint("status IN ('running', 'completed', 'stopped', 'failed')", name="status"),
        CheckConstraint(
            "finish_reason IS NULL OR finish_reason IN ('stop', 'length', 'user_cancelled', 'error')",
            name="finish_reason",
        ),
        CheckConstraint("rejected_citation_count >= 0", name="counters"),
        UniqueConstraint("message_id"),
        Index("ix_generation_runs_conversation_id", "conversation_id"),
        Index("ix_generation_runs_assistant_id", "assistant_id"),
        Index("ix_generation_runs_model_config_id", "model_config_id"),
        Index("ix_generation_runs_embedding_config_id", "embedding_config_id"),
    )

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    conversation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    message_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    assistant_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("assistants.id", ondelete="RESTRICT"), nullable=False
    )
    model_config_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("model_configs.id", ondelete="SET NULL"), nullable=True
    )
    embedding_config_id: Mapped[UUID | None] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("embedding_provider_configs.id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(Text(), nullable=False)
    finish_reason: Mapped[str | None] = mapped_column(Text(), nullable=True)
    retrieved_chunk_ids: Mapped[list[UUID]] = mapped_column(
        ARRAY(PG_UUID(as_uuid=True)), nullable=False, server_default=text("'{}'::uuid[]")
    )
    rejected_citation_count: Mapped[int] = mapped_column(Integer(), nullable=False, server_default=text("0"))
    prompt_tokens: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    completion_tokens: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    retrieval_ms: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    time_to_first_token_ms: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    total_ms: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    error_code: Mapped[str | None] = mapped_column(Text(), nullable=True)
    started_at: Mapped[datetime] = _ts(nullable=False, default_now=True)
    finished_at: Mapped[datetime | None] = _ts()
