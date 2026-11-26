"""Database roles (ADR-024): the runtime role's privileges and the check that a service does not run as owner.

``migrate`` connects as the schema owner and calls :func:`grant_runtime` after ``alembic upgrade head``. The
services connect as the runtime role: DML on the application tables, nothing else, and on the append-only
tables only what is listed in :data:`RESTRICTED_TABLES`. Every grant is rebuilt from scratch each run, so the
privileges are the same after a restore with ``--no-privileges``, after a revision that added a table and
after manual changes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

FULL_DML: Final = ("SELECT", "INSERT", "UPDATE", "DELETE")

# Tables the runtime role may not change freely. Every other table of the schema gets FULL_DML.
RESTRICTED_TABLES: Final[dict[str, tuple[str, ...]]] = {
    "audit_logs": ("SELECT", "INSERT"),  # append-only: retention runs as the owner (ADR-024 §6)
    "alembic_version": ("SELECT",),  # readiness compares it with the shipped heads
}

# The only DDL the runtime role can trigger: the schema owner's SECURITY DEFINER functions of revision 0017,
# which build or drop the HNSW index of a stored embedding profile (ADR-017 §2).
EXECUTABLE_FUNCTIONS: Final = (
    "plummy_ensure_vector_index(uuid)",
    "plummy_drop_vector_index(uuid)",
)

# Relations of the schema the owner can grant on: tables, partitioned tables, views, materialized views.
_RELATIONS = """
SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm') AND pg_has_role(c.relowner, 'USAGE')
ORDER BY c.relname
"""


class RuntimeRoleError(Exception):
    """The runtime role is missing or is not separate from the owner. The message names no credentials."""


@dataclass(frozen=True, slots=True)
class GrantReport:
    role: str
    tables: int
    restricted: tuple[str, ...]


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


async def _check_role(connection: AsyncConnection, role: str) -> None:
    row = (
        await connection.execute(
            text(
                "SELECT r.rolsuper, r.rolbypassrls, r.rolcreaterole, r.rolcreatedb, "
                "pg_has_role(r.oid, current_user, 'MEMBER') OR r.rolname = current_user "
                "FROM pg_roles r WHERE r.rolname = :role"
            ),
            {"role": role},
        )
    ).first()
    if row is None:
        raise RuntimeRoleError(
            f"database role {role!r} does not exist: the database administrator creates it once "
            "(docs/DEPLOYMENT.md §6.1, deploy/scripts/split-db-roles.sh)"
        )
    superuser, bypass_rls, create_role, create_db, owner_member = row
    if owner_member:
        raise RuntimeRoleError(f"database role {role!r} is the schema owner or a member of it (ADR-024 §1)")
    if superuser or bypass_rls or create_role or create_db:
        raise RuntimeRoleError(
            f"database role {role!r} must be NOSUPERUSER NOBYPASSRLS NOCREATEROLE NOCREATEDB (ADR-024 §1)"
        )


async def grant_runtime(engine: AsyncEngine, role: str) -> GrantReport:
    """Reset the runtime role's privileges to exactly those of ADR-024 §2, in one transaction."""
    quoted = _quote(role)
    async with engine.begin() as connection:
        await _check_role(connection, role)
        database = _quote((await connection.execute(text("SELECT current_database()"))).scalar_one())
        relations = list((await connection.execute(text(_RELATIONS))).scalars())
        statements = [
            f"REVOKE ALL ON DATABASE {database} FROM PUBLIC",
            f"REVOKE ALL ON DATABASE {database} FROM {quoted}",
            f"GRANT CONNECT ON DATABASE {database} TO {quoted}",
            "REVOKE CREATE ON SCHEMA public FROM PUBLIC",
            f"REVOKE ALL ON SCHEMA public FROM {quoted}",
            f"GRANT USAGE ON SCHEMA public TO {quoted}",
            f"REVOKE ALL ON ALL TABLES IN SCHEMA public FROM {quoted}",
            f"REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM {quoted}",
            f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {quoted}",
            f"REVOKE ALL ON ALL FUNCTIONS IN SCHEMA public FROM {quoted}",
        ]
        for function in EXECUTABLE_FUNCTIONS:
            if (
                await connection.execute(text("SELECT to_regprocedure(:f)"), {"f": f"public.{function}"})
            ).scalar():
                statements.append(f"GRANT EXECUTE ON FUNCTION public.{function} TO {quoted}")
        for relation in relations:
            privileges = RESTRICTED_TABLES.get(relation, FULL_DML)
            statements.append(f"GRANT {', '.join(privileges)} ON TABLE public.{_quote(relation)} TO {quoted}")
        for statement in statements:
            await connection.execute(text(statement))
    restricted = tuple(sorted(name for name in RESTRICTED_TABLES if name in relations))
    return GrantReport(role=role, tables=len(relations), restricted=restricted)


@dataclass(frozen=True, slots=True)
class RoleStatus:
    """What the role of this connection could do to the audit log."""

    superuser: bool
    owns_audit_log: bool
    can_change_audit_log: bool

    @property
    def separated(self) -> bool:
        return not (self.superuser or self.owns_audit_log or self.can_change_audit_log)


async def inspect_role(engine: AsyncEngine) -> RoleStatus | None:
    """``None`` before the first migration (no ``audit_logs`` table yet)."""
    async with engine.connect() as connection:
        row = (
            await connection.execute(
                text(
                    "SELECT r.rolsuper, pg_has_role(c.relowner, 'USAGE'), "
                    "has_table_privilege(c.oid, 'UPDATE') OR has_table_privilege(c.oid, 'DELETE') "
                    "OR has_table_privilege(c.oid, 'TRUNCATE') "
                    "FROM pg_roles r CROSS JOIN pg_class c "
                    "WHERE r.rolname = current_user AND c.oid = to_regclass('public.audit_logs')"
                )
            )
        ).first()
    if row is None:
        return None
    return RoleStatus(superuser=bool(row[0]), owns_audit_log=bool(row[1]), can_change_audit_log=bool(row[2]))
