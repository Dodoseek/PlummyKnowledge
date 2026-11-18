"""RBAC catalog: permissions, system roles and their assignment (SECURITY_MODEL.md §3, ADR-012).

Seed data is literal (migrations are immutable); IDs are UUIDv5 of ``plummy:permission:<key>`` /
``plummy:role:<key>`` so every installation has the same identifiers. A test compares the seed with
``plummy.domain.identity.permissions``.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-17
"""

from collections.abc import Sequence
from uuid import UUID

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

PERMISSIONS = (
    ("883085ac-958b-58f4-a66a-80383a461212", "system.settings.read", "View Instance Settings (no secrets)"),
    (
        "b7d1ad83-72db-5548-88cc-f1609c3dd897",
        "settings.manage",
        "Manage Application Settings within the instance policy",
    ),
    ("c3404b47-b71c-56d4-976d-f8ae9b6fcda1", "users.read", "View users"),
    ("9f122370-55b5-5b74-b2bb-8b6e3976f475", "users.manage", "Manage users"),
    ("f541bf28-c37a-5645-8c31-cc1f4ade042f", "groups.read", "View groups"),
    ("558bf4db-aeca-5140-aeb2-545c6bf9b931", "groups.manage", "Manage groups"),
    ("def6161e-2beb-59ed-856c-54f6ddc4057e", "roles.read", "View roles"),
    ("09254b87-b832-5d93-bd53-0305692ffbf6", "roles.assign", "Assign roles"),
    (
        "231a1747-05d8-5e7c-b314-dd118657d860",
        "providers.read",
        "View AI providers, models and embedding profiles",
    ),
    (
        "87ac9ae5-6274-5942-9b23-d31c4af839c6",
        "providers.manage",
        "Manage AI providers, models and embedding profiles",
    ),
    ("64f33608-bf69-50bd-81f3-8141b080e33f", "data_sources.read", "View data sources"),
    ("f2621355-c8b2-543b-aeec-c0dffab1b9ac", "data_sources.manage", "Connect and manage data sources"),
    ("cafcfc4b-a68f-5586-a3ef-1e140acba4a2", "data_sources.sync", "Start data source synchronization"),
    (
        "d5a39780-a017-5746-9ca1-e35cab8cb984",
        "knowledge_bases.manage",
        "Create and manage all knowledge bases",
    ),
    ("200b19a3-a2a7-5b59-86c9-850bc91ff327", "assistants.manage", "Create and configure assistants"),
    ("58c1ef80-bc34-5ed6-a85b-3edc30b40aaf", "jobs.read", "View jobs"),
    ("44e0e509-6f87-5042-b992-1c300cab1524", "jobs.manage", "Retry and cancel jobs"),
    ("46832e1e-88b2-5888-9783-490d53c704e8", "audit.read", "View the audit log"),
    ("d782deca-3619-5091-96fe-652658943cb3", "chat.use", "Chat with available assistants"),
)

ROLES = (
    (
        "649440d8-b3ef-58a3-8c4b-f951534b6911",
        "admin",
        "Administrator",
        "Manages users, roles, providers, sources, knowledge bases, assistants, settings and audit",
    ),
    (
        "f1e3d92b-5768-5d75-b05f-1d195ec55552",
        "knowledge_manager",
        "Knowledge Manager",
        "Manages documents, knowledge bases and synchronization",
    ),
    ("8ecf5923-a0e5-55dd-afdc-40dd63ba6e29", "member", "Member", "Works with permitted assistants"),
    ("596e1961-7733-5fb9-a276-991579415e27", "auditor", "Auditor", "Reads the audit log"),
)

ROLE_PERMISSIONS = {
    "admin": (
        "system.settings.read",
        "settings.manage",
        "users.read",
        "users.manage",
        "groups.read",
        "groups.manage",
        "roles.read",
        "roles.assign",
        "providers.read",
        "providers.manage",
        "data_sources.read",
        "data_sources.manage",
        "data_sources.sync",
        "knowledge_bases.manage",
        "assistants.manage",
        "jobs.read",
        "jobs.manage",
        "audit.read",
        "chat.use",
    ),
    "knowledge_manager": (
        "users.read",
        "groups.read",
        "providers.read",
        "data_sources.read",
        "data_sources.sync",
        "knowledge_bases.manage",
        "jobs.read",
        "jobs.manage",
        "chat.use",
    ),
    "member": ("chat.use",),
    "auditor": ("audit.read",),
}


def upgrade() -> None:
    permissions = op.create_table(
        "permissions",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_permissions")),
        sa.UniqueConstraint("key", name=op.f("uq_permissions_key")),
    )
    roles = op.create_table(
        "roles",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("is_system", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_roles")),
        sa.UniqueConstraint("key", name=op.f("uq_roles_key")),
    )
    role_permissions = op.create_table(
        "role_permissions",
        sa.Column("role_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("permission_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["role_id"], ["roles.id"], name=op.f("fk_role_permissions_role_id_roles"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["permission_id"],
            ["permissions.id"],
            name=op.f("fk_role_permissions_permission_id_permissions"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("role_id", "permission_id", name=op.f("pk_role_permissions")),
    )
    op.create_index("ix_role_permissions_permission_id", "role_permissions", ["permission_id"])
    op.create_table(
        "user_roles",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("role_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("granted_by", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_user_roles_user_id_users"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["role_id"], ["roles.id"], name=op.f("fk_user_roles_role_id_roles"), ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["granted_by"], ["users.id"], name=op.f("fk_user_roles_granted_by_users"), ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("user_id", "role_id", name=op.f("pk_user_roles")),
    )
    op.create_index("ix_user_roles_role_id", "user_roles", ["role_id"])

    op.bulk_insert(permissions, [{"id": UUID(i), "key": k, "description": d} for i, k, d in PERMISSIONS])
    op.bulk_insert(
        roles,
        [{"id": UUID(i), "key": k, "name": n, "description": d, "is_system": True} for i, k, n, d in ROLES],
    )
    permission_ids = {k: UUID(i) for i, k, _ in PERMISSIONS}
    role_ids = {k: UUID(i) for i, k, _, _ in ROLES}
    op.bulk_insert(
        role_permissions,
        [
            {"role_id": role_ids[role], "permission_id": permission_ids[permission]}
            for role, keys in ROLE_PERMISSIONS.items()
            for permission in keys
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_user_roles_role_id", table_name="user_roles")
    op.drop_table("user_roles")
    op.drop_index("ix_role_permissions_permission_id", table_name="role_permissions")
    op.drop_table("role_permissions")
    op.drop_table("roles")
    op.drop_table("permissions")
