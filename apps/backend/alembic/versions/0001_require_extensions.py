"""Require PostgreSQL extensions pgvector and citext (DATABASE_SCHEMA.md §9, ADR-011).

Migrations never create or drop extensions: they run with the application's database role, which has no
superuser privileges. A database administrator installs the extensions beforehand with
``deploy/postgres/extensions.sql`` (in development, the postgres container's init script does it).
This revision only verifies that they are present and fails with a clear message otherwise.

Revision ID: 0001
Revises:
Create Date: 2026-11-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

REQUIRED_EXTENSIONS = ("citext", "vector")


class MissingExtensionsError(RuntimeError):
    pass


def upgrade() -> None:
    if context.is_offline_mode():
        op.execute(f"-- requires extensions: {', '.join(REQUIRED_EXTENSIONS)} (installed by a DBA)")
        return
    installed = set(
        op.get_bind()
        .execute(
            sa.text("SELECT extname FROM pg_extension WHERE extname = ANY(:names)"),
            {"names": list(REQUIRED_EXTENSIONS)},
        )
        .scalars()
    )
    missing = [name for name in REQUIRED_EXTENSIONS if name not in installed]
    if missing:
        raise MissingExtensionsError(
            f"Required PostgreSQL extensions are not installed in this database: {', '.join(missing)}. "
            "Extensions are installed by a database administrator, not by migrations: run "
            "deploy/postgres/extensions.sql as a privileged role in this database (docs/DEPLOYMENT.md)."
        )


def downgrade() -> None:
    # Extensions are managed outside migrations (ADR-011); nothing to undo.
    pass
