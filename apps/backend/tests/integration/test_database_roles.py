"""Database roles and audit retention (ADR-024) on real PostgreSQL.

The services' runtime role reads and writes data, but cannot change the schema or rewrite the audit log;
``db grant-runtime`` resets its privileges to exactly that list; ``maintenance prune-audit`` deletes old audit
records only under the schema owner; a service refuses to start in production with a role that could rewrite
the audit log. Needs the dev stack's two roles (TEST_OWNER_DATABASE_URL and DATABASE_URL).
"""

from __future__ import annotations

import asyncio
import io
import logging
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from plummy.application.maintenance.audit_retention import PruneAuditLog
from plummy.cli.app import main
from plummy.composition import build_restore_services
from plummy.core.config import ConfigurationError, ProcessRole
from plummy.infrastructure.clock import SystemClock
from plummy.infrastructure.db.engine import create_session_factory
from plummy.infrastructure.db.roles import (
    EXECUTABLE_FUNCTIONS,
    FULL_DML,
    RESTRICTED_TABLES,
    RuntimeRoleError,
    grant_runtime,
    inspect_role,
)
from plummy.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWorkFactory
from plummy.main import check_database_role
from tests.conftest import make_settings
from tests.integration.conftest import runtime_role, runtime_url

pytestmark = pytest.mark.integration

INSUFFICIENT_PRIVILEGE = "42501"


@pytest.fixture
def role() -> str:
    name = runtime_role()
    if name is None:
        pytest.skip(
            "needs the runtime role of the dev stack (ADR-024): TEST_OWNER_DATABASE_URL and DATABASE_URL"
        )
    return name


@pytest.fixture
async def owner(migrated_database_url: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(migrated_database_url)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
async def runtime(migrated_database_url: str, role: str) -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(runtime_url(migrated_database_url))
    try:
        yield engine
    finally:
        await engine.dispose()


async def run(engine: AsyncEngine, statement: str, **params: Any) -> list[Any]:
    async with engine.begin() as connection:
        result = await connection.execute(text(statement), params)
        return list(result.all()) if result.returns_rows else []


async def denied(engine: AsyncEngine, statement: str, **params: Any) -> bool:
    """True when PostgreSQL refuses the statement for lack of privilege (not for any other error)."""
    with pytest.raises(DBAPIError) as caught:
        await run(engine, statement, **params)
    original = caught.value.orig
    return (
        getattr(original, "sqlstate", None) or getattr(original, "pgcode", None)
    ) == INSUFFICIENT_PRIVILEGE


async def insert_audit(engine: AsyncEngine, action: str, *, age_days: int = 0) -> None:
    await run(
        engine,
        "INSERT INTO audit_logs (id, actor_type, action, outcome, details, created_at) "
        "VALUES (gen_random_uuid(), 'system', :action, 'success', '{}', now() - make_interval(days => :age))",
        action=action,
        age=age_days,
    )


async def privileges(engine: AsyncEngine, role: str, table: str) -> set[str]:
    rows = await run(
        engine,
        "SELECT privilege_type FROM information_schema.role_table_grants "
        "WHERE grantee = :role AND table_schema = 'public' AND table_name = :table",
        role=role,
        table=table,
    )
    return {row[0] for row in rows}


# --- the runtime role ------------------------------------------------------------------------------------


async def test_the_runtime_role_cannot_rewrite_the_audit_log(
    owner: AsyncEngine, runtime: AsyncEngine
) -> None:
    await insert_audit(runtime, "test.appended")  # INSERT and SELECT stay allowed
    assert await run(runtime, "SELECT count(*) FROM audit_logs WHERE action = 'test.appended'") == [(1,)]
    assert await denied(runtime, "UPDATE audit_logs SET action = 'test.rewritten'")
    assert await denied(runtime, "DELETE FROM audit_logs")
    assert await denied(runtime, "TRUNCATE audit_logs")
    assert await run(owner, "SELECT count(*) FROM audit_logs WHERE action = 'test.appended'") == [(1,)]


async def test_the_runtime_role_cannot_change_the_schema(runtime: AsyncEngine) -> None:
    assert await denied(runtime, "CREATE TABLE intruder (id int)")
    assert await denied(runtime, "ALTER TABLE users ADD COLUMN intruder int")
    assert await denied(runtime, "DROP TABLE users")
    assert await denied(runtime, "ALTER TABLE audit_logs DISABLE TRIGGER ALL")
    assert await denied(runtime, "CREATE TEMPORARY TABLE scratch (id int)")
    assert await denied(runtime, "TRUNCATE users")
    assert await denied(runtime, "UPDATE alembic_version SET version_num = 'x'")
    assert await denied(runtime, "SELECT pg_read_file('/etc/hostname')")
    assert await denied(runtime, "CREATE INDEX intruder ON users (created_at)")


async def test_the_runtime_role_executes_only_the_vector_index_functions(
    owner: AsyncEngine, runtime: AsyncEngine, role: str
) -> None:
    rows = await run(
        owner,
        "SELECT p.oid::regprocedure::text, has_function_privilege(:role, p.oid, 'EXECUTE') "
        "FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' "
        "AND p.proname LIKE 'plummy_%' ORDER BY 1",
        role=role,
    )
    assert rows == [(f"{name.split('(')[0]}(uuid)", True) for name in sorted(EXECUTABLE_FUNCTIONS)]
    # Every SECURITY DEFINER function of the schema is on the list, and PUBLIC cannot execute it.
    definers = await run(
        owner,
        "SELECT p.oid::regprocedure::text, EXISTS (SELECT 1 FROM "
        "aclexplode(coalesce(p.proacl, acldefault('f', p.proowner))) a "
        "WHERE a.grantee = 0 AND a.privilege_type = 'EXECUTE') FROM pg_proc p "
        "JOIN pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = 'public' AND p.prosecdef ORDER BY 1",
    )
    assert definers == [(name, False) for name in sorted(EXECUTABLE_FUNCTIONS)]


async def test_the_runtime_role_has_data_access(runtime: AsyncEngine) -> None:
    [(count,)] = await run(runtime, "SELECT count(*) FROM permissions")
    assert count > 0
    await run(
        runtime,
        "INSERT INTO groups (id, name, created_at, updated_at) VALUES (gen_random_uuid(), 'g', now(), now())",
    )
    await run(runtime, "UPDATE groups SET name = 'h' WHERE name = 'g'")
    await run(runtime, "SELECT id FROM groups WHERE name = 'h' FOR UPDATE")
    await run(runtime, "DELETE FROM groups WHERE name = 'h'")
    assert await run(runtime, "SELECT version_num IS NOT NULL FROM alembic_version") == [(True,)]


async def test_grant_runtime_resets_privileges_and_covers_new_tables(
    owner: AsyncEngine, runtime: AsyncEngine, role: str
) -> None:
    await run(owner, f'GRANT UPDATE, DELETE, TRUNCATE ON audit_logs TO "{role}"')  # granted by hand
    await run(owner, f'GRANT TRUNCATE, TRIGGER ON users TO "{role}"')
    await run(owner, "CREATE TABLE later_revision (id int PRIMARY KEY)")  # a table added by a later revision
    assert await denied(runtime, "SELECT * FROM later_revision")

    report = await grant_runtime(owner, role)
    again = await grant_runtime(owner, role)  # idempotent
    assert report == again
    assert report.restricted == ("alembic_version", "audit_logs")
    assert await privileges(owner, role, "audit_logs") == set(RESTRICTED_TABLES["audit_logs"])
    assert await privileges(owner, role, "users") == set(FULL_DML)
    assert await privileges(owner, role, "later_revision") == set(FULL_DML)
    assert await denied(runtime, "DELETE FROM audit_logs")
    await run(runtime, "INSERT INTO later_revision VALUES (1)")
    [(tables,)] = await run(
        owner,
        "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
        "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'v', 'm')",
    )
    assert report.tables == tables


async def test_grant_runtime_refuses_a_missing_role_and_the_owner(owner: AsyncEngine, role: str) -> None:
    with pytest.raises(RuntimeRoleError, match="does not exist"):
        await grant_runtime(owner, "plummy_no_such_role")
    [(owner_role,)] = await run(owner, "SELECT current_user")
    with pytest.raises(RuntimeRoleError, match="schema owner"):
        await grant_runtime(owner, owner_role)


async def test_inspect_role_tells_the_owner_from_the_runtime_role(
    owner: AsyncEngine, runtime: AsyncEngine, bare_database_url: str
) -> None:
    owner_status = await inspect_role(owner)
    assert owner_status is not None
    assert owner_status.owns_audit_log
    assert not owner_status.separated
    runtime_status = await inspect_role(runtime)
    assert runtime_status is not None
    assert runtime_status.separated
    bare = create_async_engine(bare_database_url)
    try:
        assert await inspect_role(bare) is None  # not migrated: left to readiness
    finally:
        await bare.dispose()


# --- the startup check -----------------------------------------------------------------------------------


async def test_a_production_service_refuses_the_owner_role(
    owner: AsyncEngine, runtime: AsyncEngine, migrated_database_url: str, role: str
) -> None:
    production = make_settings(
        app_env="production",
        database_url=migrated_database_url,
        process_role="ingestion",
        session_cookie_secure=True,
    )
    with pytest.raises(ConfigurationError, match="runtime role"):
        await check_database_role(production, owner)
    await check_database_role(production, runtime)  # separated: starts
    migrate = make_settings(
        app_env="production",
        database_url=migrated_database_url,
        process_role="migrate",
        session_cookie_secure=True,
    )
    assert migrate.process_role is ProcessRole.MIGRATE
    await check_database_role(migrate, owner)  # the owner by design


async def test_outside_production_the_owner_role_only_warns(
    owner: AsyncEngine, migrated_database_url: str, caplog: pytest.LogCaptureFixture
) -> None:
    development = make_settings(app_env="development", database_url=migrated_database_url)
    with caplog.at_level(logging.WARNING, logger="plummy"):
        await check_database_role(development, owner)
    assert "runtime role" in caplog.text
    assert "password" not in caplog.text.lower()


async def test_an_unreachable_database_is_left_to_readiness(caplog: pytest.LogCaptureFixture) -> None:
    url = "postgresql+asyncpg://nobody:nothing@127.0.0.1:1/none"
    production = make_settings(
        app_env="production", database_url=url, process_role="ingestion", session_cookie_secure=True
    )
    engine = create_async_engine(url)
    try:
        with caplog.at_level(logging.WARNING, logger="plummy"):
            await check_database_role(production, engine)
    finally:
        await engine.dispose()
    assert "database unavailable" in caplog.text


# --- audit retention -------------------------------------------------------------------------------------


def prune_use_case(engine: AsyncEngine, *, batch: int = 2) -> PruneAuditLog:
    return PruneAuditLog(
        SqlAlchemyUnitOfWorkFactory(create_session_factory(engine)), SystemClock(), batch=batch
    )


async def actions(engine: AsyncEngine) -> list[str]:
    return [row[0] for row in await run(engine, "SELECT action FROM audit_logs ORDER BY created_at, action")]


async def test_prune_audit_deletes_only_old_records_and_records_the_run(owner: AsyncEngine) -> None:
    await run(owner, "DELETE FROM audit_logs")
    for number in range(5):
        await insert_audit(owner, f"test.old.{number}", age_days=400 + number)
    await insert_audit(owner, "test.boundary", age_days=364)
    await insert_audit(owner, "test.recent")

    dry = await prune_use_case(owner).execute(timedelta(days=365), dry_run=True)
    assert (dry.records, dry.dry_run) == (5, True)
    assert len(await actions(owner)) == 7  # a dry run changes nothing and records nothing

    report = await prune_use_case(owner, batch=2).execute(timedelta(days=365))  # three batches
    assert report.records == 5
    assert await actions(owner) == ["test.boundary", "test.recent", "maintenance.audit_pruned"]
    [(details,)] = await run(
        owner, "SELECT details FROM audit_logs WHERE action = 'maintenance.audit_pruned'"
    )
    assert details == {"cutoff": report.cutoff.isoformat(), "deleted": 5}

    again = await prune_use_case(owner).execute(timedelta(days=365))
    assert again.records == 0


async def test_prune_audit_is_refused_under_the_runtime_role(
    owner: AsyncEngine, runtime: AsyncEngine
) -> None:
    await insert_audit(owner, "test.old", age_days=500)
    with pytest.raises(DBAPIError):
        await prune_use_case(runtime).execute(timedelta(days=365))
    assert "test.old" in await actions(owner)


def cli(settings: Any, *argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(
        list(argv), maintenance_factory=build_restore_services, settings=settings, stdout=out, stderr=err
    )
    return code, out.getvalue(), err.getvalue()


async def test_the_cli_commands(owner: AsyncEngine, migrated_database_url: str, role: str) -> None:
    await insert_audit(owner, "test.old", age_days=500)
    as_owner = make_settings(database_url=migrated_database_url, audit_retention_days=365)
    as_runtime = make_settings(database_url=runtime_url(migrated_database_url))

    code, _, err = await asyncio.to_thread(cli, as_runtime, "maintenance", "prune-audit")
    assert code == 1
    assert "migrate container" in err
    assert "schema owner" in err
    code, _, err = await asyncio.to_thread(cli, as_owner, "maintenance", "prune-audit", "--dry-run")
    assert (code, "1 would be deleted" in err) == (0, True)
    code, _, err = await asyncio.to_thread(cli, as_owner, "maintenance", "prune-audit")
    assert (code, "1 deleted" in err, "365 days" in err) == (0, True, True)
    assert "test.old" not in await actions(owner)

    code, _, err = await asyncio.to_thread(cli, as_owner, "db", "grant-runtime")
    assert code == 1
    assert "DATABASE_RUNTIME_ROLE" in err
    with_role = make_settings(database_url=migrated_database_url, database_runtime_role=role)
    code, out, _ = await asyncio.to_thread(cli, with_role, "db", "grant-runtime")
    assert code == 0
    assert f"Privileges of {role}" in out
    assert "audit_logs" in out
    missing = make_settings(database_url=migrated_database_url, database_runtime_role="plummy_no_such_role")
    code, _, err = await asyncio.to_thread(cli, missing, "db", "grant-runtime")
    assert code == 1
    assert "split-db-roles.sh" in err
    assert migrated_database_url.split("@")[0].split(":")[-1] not in err  # no password in messages
