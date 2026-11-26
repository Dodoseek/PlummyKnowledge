"""Retention of document versions (DATABASE_SCHEMA.md §8, ADR-023 §7): real PostgreSQL, Redis and storage.

A Drive file changed several times gets one version per content; the prune keeps the current version and the
newest ``KEEP_OUTDATED`` outdated ones and removes the rest with their chunks, vectors and stored files.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from plummy.application.ingestion.maintenance import KEEP_OUTDATED, PruneDocumentVersions, PruneReport
from plummy.application.storage import StorageArea
from plummy.infrastructure.clock import SystemClock
from plummy.infrastructure.db.engine import create_engine, create_session_factory
from plummy.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWorkFactory
from tests.integration import test_source_sync
from tests.integration.test_source_sync import (
    ALPHA,
    Env,
    Tree,
    _keyword_hits,
    _process,
    build_tree,
    initial,
    set_content,
    sync_now,
)

pytestmark = pytest.mark.integration

env = test_source_sync.env  # the fixture: a Drive source on a fake Drive


@asynccontextmanager
async def pruner(env: Env) -> AsyncIterator[PruneDocumentVersions]:
    engine = create_engine(env.harness.settings, application_name="plummy-test-worker")
    try:
        uow = SqlAlchemyUnitOfWorkFactory(create_session_factory(engine))
        yield PruneDocumentVersions(uow, env.harness.resources.storage, SystemClock())
    finally:
        await engine.dispose()


async def prune(env: Env) -> PruneReport:
    async with pruner(env) as use_case:
        return await use_case.execute()


async def versions_of(env: Env, external_id: str) -> list[Any]:
    """(version_number, status, storage_key, extracted_storage_key, chunks) of the live document."""
    return await env.harness.sql(
        "SELECT v.version_number, v.status, v.storage_key, v.extracted_storage_key, "
        "(SELECT count(*) FROM document_chunks c WHERE c.document_version_id = v.id) "
        "FROM document_versions v JOIN documents d ON d.id = v.document_id "
        "WHERE d.external_id = :ext AND d.deleted_at IS NULL ORDER BY v.version_number",
        ext=external_id,
    )


async def stored_keys(env: Env) -> set[str]:
    storage = env.harness.resources.storage
    keys: set[str] = set()
    for area in StorageArea:
        keys |= {key.value async for key in storage.keys(area)}
    return keys


async def changed_five_times(env: Env) -> tuple[Tree, str]:
    """Versions 1-6 of ``alpha``, every one indexed: 6 is current, 1-5 are outdated."""
    tree = build_tree(env.google)
    source_id = await initial(env, tree)
    await _process(env)
    for number in range(2, 7):
        set_content(env.google, tree.alpha, f"Alpha policy, edition {number}: leave is 28 days.\n".encode())
        env.google.change(tree.alpha)
        await sync_now(env, source_id)
        await _process(env)
    return tree, source_id


async def test_old_outdated_versions_are_removed_with_their_data(env: Env) -> None:
    tree, source_id = await changed_five_times(env)
    before = await versions_of(env, tree.alpha)
    assert [(row[0], row[1]) for row in before] == [
        (1, "outdated"),
        (2, "outdated"),
        (3, "outdated"),
        (4, "outdated"),
        (5, "outdated"),
        (6, "ready"),
    ]
    removed_files = {key for row in before[:2] for key in (row[2], row[3]) if key is not None}
    kept_files = {key for row in before[2:] for key in (row[2], row[3]) if key is not None}
    assert removed_files <= await stored_keys(env)

    report = await prune(env)
    assert (report.documents, report.versions) == (1, 6 - 1 - KEEP_OUTDATED)
    after = await versions_of(env, tree.alpha)
    assert [(row[0], row[1]) for row in after] == [
        (3, "outdated"),
        (4, "outdated"),
        (5, "outdated"),
        (6, "ready"),
    ]
    assert all(row[4] > 0 for row in after)  # the kept versions keep their chunks
    stored = await stored_keys(env)
    assert removed_files.isdisjoint(stored)
    assert kept_files <= stored
    [(orphans,)] = await env.harness.sql(
        "SELECT count(*) FROM embeddings e WHERE NOT EXISTS "
        "(SELECT 1 FROM document_versions v WHERE v.id = e.document_version_id)"
    )
    assert orphans == 0
    assert await _keyword_hits(env, source_id, "edition") > 0  # the current version is still found

    assert await prune(env) == PruneReport()  # nothing left to do
    # The same content as a removed version is a new version now, not a lookup of the removed one.
    set_content(env.google, tree.alpha, ALPHA)
    env.google.change(tree.alpha)
    await sync_now(env, source_id)
    await _process(env)
    latest = (await versions_of(env, tree.alpha))[-1]
    assert (latest[0], latest[1]) == (7, "ready")


async def test_documents_with_a_running_job_are_left_for_later(env: Env) -> None:
    tree, _ = await changed_five_times(env)
    [(document_id,)] = await env.harness.sql(
        "SELECT id FROM documents WHERE external_id = :ext AND deleted_at IS NULL", ext=tree.alpha
    )
    reindex = await env.admin.post(f"/api/v1/documents/{document_id}/reindex")
    assert reindex.status_code == 202, reindex.text
    assert (await prune(env)).versions == 0
    assert len(await versions_of(env, tree.alpha)) == 6


async def test_an_interrupted_prune_is_finished_by_the_next_run(env: Env) -> None:
    tree, _ = await changed_five_times(env)
    # The first step of an earlier run committed (version 1 left lookups), its files and row are still there.
    await env.harness.sql(
        "UPDATE document_versions SET status = 'deleted', active_chunk_set = NULL "
        "WHERE version_number = 1 AND document_id IN (SELECT id FROM documents WHERE external_id = :ext)",
        ext=tree.alpha,
    )
    [first] = (await versions_of(env, tree.alpha))[:1]
    report = await prune(env)
    assert report.versions == 2  # the leftover and the one beyond the kept outdated versions
    assert [row[0] for row in await versions_of(env, tree.alpha)] == [3, 4, 5, 6]
    assert first[2] not in await stored_keys(env)


async def test_unused_chunk_sets_are_removed_but_active_ones_stay(env: Env) -> None:
    tree = build_tree(env.google)
    await initial(env, tree)
    await _process(env)
    # A chunk set that no version uses any more (left by an earlier reindex of the version).
    await env.harness.sql(
        "INSERT INTO document_chunks (id, document_id, document_version_id, data_source_id, chunk_set, "
        "chunk_index, content, content_hash, token_count, page_start, page_end, section_path, sheet_name, "
        "row_start, row_end) "
        "SELECT gen_random_uuid(), c.document_id, c.document_version_id, c.data_source_id, c.chunk_set + 1, "
        "c.chunk_index, c.content, c.content_hash, c.token_count, c.page_start, c.page_end, c.section_path, "
        "c.sheet_name, c.row_start, c.row_end FROM document_chunks c"
    )
    [(total,)] = await env.harness.sql("SELECT count(*) FROM document_chunks")
    report = await prune(env)
    assert report.inactive_chunks == total // 2
    [(left,)] = await env.harness.sql("SELECT count(*) FROM document_chunks")
    [(sets,)] = await env.harness.sql(
        "SELECT count(*) FROM document_chunks c JOIN document_versions v ON v.id = c.document_version_id "
        "WHERE c.chunk_set = v.active_chunk_set"
    )
    assert left == sets == total // 2
