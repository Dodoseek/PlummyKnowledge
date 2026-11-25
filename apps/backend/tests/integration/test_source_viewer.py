"""``GET /api/v1/documents/{id}/versions/{vid}/chunks/{cid}/view`` (API_CONTRACTS.md §2.11, SC-05).

The viewer checks access to a knowledge base of the document on every request and only serves a chunk of
that very version of that very document.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest

from tests.integration.harness import running_auth_app
from tests.integration.test_document_upload import Env, make_env
from tests.integration.test_knowledge_bases_api import signed_in
from tests.integration.test_retrieval import TextEmbedder, create_kb, grant, indexed

pytestmark = pytest.mark.integration

SECTIONS = 7
HANDBOOK = "\n\n".join(
    f"## Section {number}\n\nParagraph {number} explains rule number {number} of the handbook."
    for number in range(1, SECTIONS + 1)
).encode()
HOSTILE = b"# Note\n\n<script>alert('x')</script> <img src=x onerror=alert(1)> [link](javascript:alert(1))"


@dataclass
class Viewer:
    env: Env
    reader: httpx.AsyncClient
    reader_id: str
    document_id: str
    version_id: str
    chunks: list[tuple[str, str]]  # (id, content) by chunk_index


@pytest.fixture
async def env(
    migrated_database_url: str, redis_url: str, clean_rate_limits: None, tmp_path: Path
) -> AsyncIterator[Env]:
    async with running_auth_app(migrated_database_url, redis_url, tmp_path / "bootstrap") as harness:
        yield await make_env(harness)


async def chunks_of(env: Env, document_id: str) -> tuple[str, list[tuple[str, str]]]:
    rows = await env.harness.sql(
        "SELECT document_version_id, id, content FROM document_chunks WHERE document_id = :id "
        "ORDER BY chunk_index",
        id=document_id,
    )
    return str(rows[0][0]), [(str(chunk_id), content) for _, chunk_id, content in rows]


@pytest.fixture
async def viewer(env: Env) -> Viewer:
    document_id = await indexed(env, env.kb_id, "handbook.md", HANDBOOK, TextEmbedder())
    version_id, chunks = await chunks_of(env, document_id)
    assert len(chunks) >= 5, "the handbook needs neighbours on both sides of a middle chunk"
    reader, reader_id = await signed_in(env, "reader@example.com", [])
    await grant(env, env.kb_id, reader_id)
    return Viewer(env, reader, reader_id, document_id, version_id, chunks)


def url(document_id: str, version_id: str, chunk_id: str) -> str:
    return f"/api/v1/documents/{document_id}/versions/{version_id}/chunks/{chunk_id}/view"


async def view(viewer: Viewer, index: int, client: httpx.AsyncClient | None = None) -> httpx.Response:
    address = url(viewer.document_id, viewer.version_id, viewer.chunks[index][0])
    return await (client or viewer.reader).get(address)


def not_found(response: httpx.Response) -> None:
    assert response.status_code == 404, response.text
    payload: dict[str, Any] = response.json()
    assert payload["code"] == "not_found"
    assert payload["detail"] == "Document not found."


async def test_a_cited_chunk_with_two_neighbours_on_each_side(viewer: Viewer) -> None:
    middle = len(viewer.chunks) // 2
    response = await view(viewer, middle)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["document"] == {
        "id": viewer.document_id,
        "filename": "handbook.md",
        "mime_type": "text/markdown",
    }
    assert body["version"] == {"id": viewer.version_id, "version_number": 1, "is_current": True}
    chunk_id, content = viewer.chunks[middle]
    assert body["chunk"]["id"] == chunk_id
    assert body["chunk"]["content"] == content
    assert body["chunk"]["section_path"]
    contents = [text for _, text in viewer.chunks]
    assert body["context"] == {
        "before": contents[middle - 2 : middle],
        "after": contents[middle + 1 : middle + 3],
    }


async def test_the_first_and_last_chunks_have_context_on_one_side(viewer: Viewer) -> None:
    first = (await view(viewer, 0)).json()["context"]
    assert first["before"] == []
    assert len(first["after"]) == 2
    last = (await view(viewer, len(viewer.chunks) - 1)).json()["context"]
    assert len(last["before"]) == 2
    assert last["after"] == []


async def test_without_access_the_chunk_does_not_exist(viewer: Viewer) -> None:
    stranger, _ = await signed_in(viewer.env, "stranger@example.com", ["member"])
    not_found(await view(viewer, 0, stranger))
    # Access is checked on every request: a revoked grant closes a passage seen a moment ago.
    assert (await view(viewer, 0)).status_code == 200
    await viewer.env.harness.sql("DELETE FROM knowledge_base_access WHERE user_id = :id", id=viewer.reader_id)
    not_found(await view(viewer, 0))
    anonymous = viewer.env.harness.new_client()
    address = url(viewer.document_id, viewer.version_id, viewer.chunks[0][0])
    assert (await anonymous.get(address)).status_code == 401


async def test_a_deleted_document_is_gone_from_the_viewer(viewer: Viewer) -> None:
    deleted = await viewer.env.admin.delete(f"/api/v1/documents/{viewer.document_id}")
    assert deleted.status_code == 204, deleted.text
    not_found(await view(viewer, 0))
    not_found(await view(viewer, 0, viewer.env.admin))


async def test_a_chunk_is_served_only_for_its_own_document_and_version(viewer: Viewer) -> None:
    env = viewer.env
    other_kb = await create_kb(env, "Finance")
    salaries = b"# Salaries\n\nThe review is in March."
    secret_id = await indexed(env, other_kb, "salaries.md", salaries, TextEmbedder())
    secret_version, [(secret_chunk, _)] = await chunks_of(env, secret_id)
    own_chunk = viewer.chunks[0][0]
    for document_id, version_id, chunk_id in (
        # The secret chunk through a document the reader may open.
        (viewer.document_id, viewer.version_id, secret_chunk),
        (viewer.document_id, secret_version, secret_chunk),
        # A chunk of the readable document through another version or document.
        (viewer.document_id, secret_version, own_chunk),
        (secret_id, secret_version, secret_chunk),
        (str(uuid4()), viewer.version_id, own_chunk),
        (viewer.document_id, str(uuid4()), own_chunk),
        (viewer.document_id, viewer.version_id, str(uuid4())),
    ):
        not_found(await viewer.reader.get(url(document_id, version_id, chunk_id)))
    # The administrator manages every knowledge base and sees the secret passage only at its own address.
    assert (await env.admin.get(url(secret_id, secret_version, secret_chunk))).status_code == 200
    not_found(await env.admin.get(url(viewer.document_id, viewer.version_id, secret_chunk)))


async def test_malformed_ids_are_rejected(viewer: Viewer) -> None:
    response = await viewer.reader.get(url("nope", viewer.version_id, viewer.chunks[0][0]))
    assert response.status_code == 422


async def test_markup_in_a_document_is_returned_as_plain_data(viewer: Viewer) -> None:
    env = viewer.env
    document_id = await indexed(env, env.kb_id, "note.md", HOSTILE, TextEmbedder())
    version_id, [(chunk_id, _)] = await chunks_of(env, document_id)
    response = await viewer.reader.get(url(document_id, version_id, chunk_id))
    assert response.headers["content-type"] == "application/json"
    content = response.json()["chunk"]["content"]
    assert "<script>alert('x')</script>" in content  # the client renders it as text, never as HTML
