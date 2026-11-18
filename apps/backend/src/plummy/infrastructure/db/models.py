"""ORM models for system and identity tables (DATABASE_SCHEMA.md §3, §7.3).

ORM entities never leave the infrastructure layer: repositories map them to domain dataclasses.
Every change here needs an Alembic revision; a test compares the models with the migrated schema.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    SmallInteger,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import CITEXT, INET, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column

from plummy.infrastructure.db.base import Base

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
