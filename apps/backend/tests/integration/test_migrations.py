from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from plummy.infrastructure.db.migrations import get_expected_heads
from tests.integration.conftest import Migrator

pytestmark = pytest.mark.integration


async def _query(database_url: str, sql: str) -> list[object]:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            return list((await connection.execute(text(sql))).scalars().all())
    finally:
        await engine.dispose()


async def _extensions(database_url: str) -> set[object]:
    return set(await _query(database_url, "SELECT extname FROM pg_extension"))


async def _alembic_version_exists(database_url: str) -> bool:
    return await _query(database_url, "SELECT to_regclass('public.alembic_version') IS NOT NULL") == [True]


# --- least privilege (ADR-011) ----------------------------------------------------------------------------


async def test_application_role_is_not_a_superuser(database_url: str) -> None:
    rows = await _query(database_url, "SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
    assert rows == [False], "migrations and the API must not run as a PostgreSQL superuser"


async def test_application_role_cannot_create_untrusted_objects(database_url: str) -> None:
    # A role without superuser privileges cannot, e.g., create an untrusted language or read server files.
    with pytest.raises(DBAPIError, match="permission denied"):
        await _query(database_url, "SELECT pg_read_file('/etc/hostname')")


# --- migrations -------------------------------------------------------------------------------------------


async def test_upgrade_on_prepared_database_reaches_head(migrator: Migrator, database_url: str) -> None:
    assert {"vector", "citext"} <= await _extensions(database_url)  # installed by the DBA step

    await migrator.upgrade()

    assert set(await _query(database_url, "SELECT version_num FROM alembic_version")) == set(
        get_expected_heads()
    )
    # pgvector and citext are usable by the application role.
    assert await _query(database_url, "SELECT '[1,2,3]'::vector <-> '[1,2,4]'::vector") == [1.0]
    assert await _query(database_url, "SELECT 'ABC'::citext = 'abc'::citext") == [True]


async def test_upgrade_fails_clearly_when_extensions_are_missing(
    bare_database_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    migrator = Migrator(bare_database_url, monkeypatch)
    assert {"vector", "citext"}.isdisjoint(await _extensions(bare_database_url))

    expected = r"not installed in this database: citext, vector.*extensions\.sql"
    with pytest.raises(RuntimeError, match=expected):
        await migrator.upgrade()

    # Nothing was created, and the migration did not try to install the extensions itself.
    assert {"vector", "citext"}.isdisjoint(await _extensions(bare_database_url))
    # Transactional DDL: the failed run leaves no version table behind.
    assert not await _alembic_version_exists(bare_database_url)


async def test_upgrade_is_idempotent(migrator: Migrator, database_url: str) -> None:
    await migrator.upgrade()
    await migrator.upgrade()
    assert len(await _query(database_url, "SELECT version_num FROM alembic_version")) == 1


async def test_downgrade_to_base_keeps_extensions_and_upgrade_again(
    migrator: Migrator, database_url: str
) -> None:
    await migrator.upgrade()
    await migrator.downgrade("base")

    # Extensions belong to the DBA, not to migrations: downgrade never drops them.
    assert {"vector", "citext"} <= await _extensions(database_url)
    assert await _query(database_url, "SELECT count(*) FROM alembic_version") == [0]

    await migrator.upgrade()
    assert len(await _query(database_url, "SELECT version_num FROM alembic_version")) == 1


async def test_no_application_tables_are_created_yet(migrator: Migrator, database_url: str) -> None:
    await migrator.upgrade()
    tables = await _query(
        database_url, "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
    )
    assert tables == ["alembic_version"]
