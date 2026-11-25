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


async def test_identity_tables_are_created(migrator: Migrator, database_url: str) -> None:
    await migrator.upgrade()
    tables = await _query(
        database_url, "SELECT tablename FROM pg_tables WHERE schemaname = 'public' ORDER BY tablename"
    )
    assert tables == [
        "ai_providers",
        "alembic_version",
        "assistant_knowledge_bases",
        "assistants",
        "audit_logs",
        "conversations",
        "data_sources",
        "document_chunks",
        "document_versions",
        "documents",
        "embedding_provider_configs",
        "embeddings",
        "generation_runs",
        "group_roles",
        "groups",
        "ingestion_jobs",
        "instance_state",
        "knowledge_base_access",
        "knowledge_base_sources",
        "knowledge_bases",
        "message_citations",
        "messages",
        "model_configs",
        "permissions",
        "role_permissions",
        "roles",
        "secret_references",
        "sessions",
        "user_activation_tokens",
        "user_groups",
        "user_roles",
        "users",
    ]
    # Single-organization deployment (ADR-012): no tenant table and no tenant column anywhere.
    assert "organizations" not in tables
    columns = await _query(
        database_url,
        "SELECT column_name FROM information_schema.columns WHERE table_schema = 'public' "
        "AND column_name = 'organization_id'",
    )
    assert columns == []


async def test_models_match_migrated_schema(migrator: Migrator, database_url: str) -> None:
    from alembic.autogenerate import compare_metadata
    from alembic.migration import MigrationContext

    from plummy.infrastructure.db import models  # noqa: F401 - registers the tables
    from plummy.infrastructure.db.base import Base
    from plummy.infrastructure.db.migrations import include_object

    await migrator.upgrade()
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            # a runtime index of one embedding profile (ADR-017 §2) is not part of the migrated schema
            await connection.execute(
                text(
                    "CREATE INDEX ix_embeddings_hnsw_0123456789abcdef ON embeddings "
                    "USING hnsw ((embedding::vector(3)) vector_cosine_ops) "
                    "WHERE embedding_config_id = '00000000-0000-0000-0000-000000000000'"
                )
            )
            diff = await connection.run_sync(
                lambda sync: compare_metadata(
                    MigrationContext.configure(
                        sync, opts={"compare_type": True, "include_object": include_object}
                    ),
                    Base.metadata,
                )
            )
    finally:
        await engine.dispose()
    assert diff == []


async def test_rbac_seed_matches_domain_catalog(migrator: Migrator, database_url: str) -> None:
    from plummy.domain.identity import SYSTEM_ROLE_PERMISSIONS, Permission

    await migrator.upgrade()
    permissions = set(await _query(database_url, "SELECT key FROM permissions"))
    assert permissions == {p.value for p in Permission}
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            rows = (
                await connection.execute(
                    text(
                        "SELECT r.key, p.key FROM role_permissions rp JOIN roles r ON r.id = rp.role_id "
                        "JOIN permissions p ON p.id = rp.permission_id"
                    )
                )
            ).all()
    finally:
        await engine.dispose()
    seeded: dict[str, set[str]] = {}
    for role, permission in rows:
        seeded.setdefault(role, set()).add(permission)
    assert seeded == {role.value: {p.value for p in perms} for role, perms in SYSTEM_ROLE_PERMISSIONS.items()}


async def test_instance_state_is_a_singleton(migrator: Migrator, database_url: str) -> None:
    await migrator.upgrade()
    assert await _query(database_url, "SELECT count(*) FROM instance_state WHERE initialized_at IS NULL") == [
        1
    ]
    with pytest.raises(DBAPIError, match=r"ck_instance_state_singleton|duplicate key"):
        await _query(database_url, "INSERT INTO instance_state (id) VALUES (2) RETURNING id")


async def test_upgrade_from_foundation_revision(migrator: Migrator, database_url: str) -> None:
    await migrator.upgrade("0001")  # the Foundation release schema
    await migrator.upgrade()
    assert await _query(database_url, "SELECT count(*) FROM roles") == [4]
    await migrator.downgrade("0001")
    tables = await _query(database_url, "SELECT tablename FROM pg_tables WHERE schemaname = 'public'")
    assert tables == ["alembic_version"]


async def test_chunk_search_vector_is_generated_and_indexed(migrator: Migrator, database_url: str) -> None:
    # ADR-018: PostgreSQL computes the column with the russian configuration; the GIN index is valid.
    await migrator.upgrade()
    assert await _query(
        database_url,
        "SELECT attgenerated::text || ':' || pg_get_expr(d.adbin, d.adrelid) FROM pg_attribute a "
        "JOIN pg_attrdef d ON d.adrelid = a.attrelid AND d.adnum = a.attnum "
        "WHERE a.attrelid = 'document_chunks'::regclass AND a.attname = 'search_vector'",
    ) == ["s:to_tsvector('russian'::regconfig, content)"]
    assert await _query(
        database_url,
        "SELECT i.indisvalid AND am.amname = 'gin' FROM pg_class c JOIN pg_index i ON i.indexrelid = c.oid "
        "JOIN pg_am am ON am.oid = c.relam WHERE c.relname = 'ix_document_chunks_search_vector'",
    ) == [True]


async def test_chunk_search_vector_downgrade_and_upgrade_again(migrator: Migrator, database_url: str) -> None:
    await migrator.upgrade()
    await migrator.downgrade("0010")
    assert await _query(
        database_url,
        "SELECT count(*) FROM pg_attribute WHERE attrelid = 'document_chunks'::regclass "
        "AND attname = 'search_vector' AND NOT attisdropped",
    ) == [0]
    index_exists = "SELECT to_regclass('ix_document_chunks_search_vector') IS NOT NULL"
    assert await _query(database_url, index_exists) == [False]
    await migrator.upgrade()
    assert await _query(database_url, index_exists) == [True]


async def test_assistant_tables_link_and_protect(migrator: Migrator, database_url: str) -> None:
    # DATABASE_SCHEMA.md §5.3: a profile in use cannot be deleted, a model leaves deleted assistants;
    # links go with either side.
    await migrator.upgrade()
    actions = await _query(
        database_url,
        "SELECT conname::text || ':' || confdeltype::text FROM pg_constraint "
        "WHERE contype = 'f' AND conrelid IN ('assistants'::regclass, 'assistant_knowledge_bases'::regclass) "
        "ORDER BY conname",
    )
    assert actions == [
        "fk_assistant_knowledge_bases_assistant_id_assistants:c",
        "fk_assistant_knowledge_bases_knowledge_base_id_knowledge_bases:c",
        "fk_assistants_created_by_users:n",
        "fk_assistants_embedding_config_id_embedding_provider_configs:r",
        "fk_assistants_model_config_id_model_configs:n",
    ]
    assert await _query(
        database_url,
        "SELECT indexdef LIKE '%WHERE (deleted_at IS NULL)%' FROM pg_indexes "
        "WHERE indexname = 'uq_assistants_name_active'",
    ) == [True]


async def test_assistant_tables_downgrade_and_upgrade_again(migrator: Migrator, database_url: str) -> None:
    await migrator.upgrade()
    await migrator.downgrade("0012")
    exists = (
        "SELECT to_regclass('assistants') IS NOT NULL OR to_regclass('assistant_knowledge_bases') IS NOT NULL"
    )
    assert await _query(database_url, exists) == [False]
    await migrator.upgrade()
    assert await _query(database_url, exists) == [True]


async def test_chat_tables_keep_history_apart_from_documents(migrator: Migrator, database_url: str) -> None:
    # DATABASE_SCHEMA.md §6: conversations go with their user, history never blocks deleting a model or a
    # profile, and citations do not reference documents (a purge leaves the snapshot).
    await migrator.upgrade()
    actions = await _query(
        database_url,
        "SELECT conname::text || ':' || confdeltype::text FROM pg_constraint WHERE contype = 'f' AND conrelid IN "
        "('conversations'::regclass, 'messages'::regclass, 'message_citations'::regclass, "
        "'generation_runs'::regclass) ORDER BY conname",
    )
    assert actions == [
        "fk_conversations_assistant_id_assistants:r",
        "fk_conversations_user_id_users:c",
        "fk_generation_runs_assistant_id_assistants:r",
        "fk_generation_runs_conversation_id_conversations:c",
        "fk_generation_runs_embedding_config_id_embedding_provid_c1b8:n",  # shortened to 63 characters
        "fk_generation_runs_message_id_messages:c",
        "fk_generation_runs_model_config_id_model_configs:n",
        "fk_message_citations_message_id_messages:c",
        "fk_messages_conversation_id_conversations:c",
    ]
    assert await _query(
        database_url,
        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conname = 'uq_messages_conversation_id_client_message_id'",
    ) == ["UNIQUE (conversation_id, client_message_id)"]


async def test_chat_tables_downgrade_and_upgrade_again(migrator: Migrator, database_url: str) -> None:
    await migrator.upgrade()
    await migrator.downgrade("0013")
    exists = "SELECT to_regclass('conversations') IS NOT NULL OR to_regclass('generation_runs') IS NOT NULL"
    assert await _query(database_url, exists) == [False]
    await migrator.upgrade()
    assert await _query(database_url, exists) == [True]
