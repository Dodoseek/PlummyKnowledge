"""Upload intake and ingestion stages 2-7 on real PostgreSQL, Redis and local storage.

API_CONTRACTS.md §2.11, ADR-008, SECURITY_MODEL.md §9: access before the file is read, size limit while
streaming, type by content, server-generated storage keys, a job message with the job ID only.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from collections.abc import AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest

from plummy.application.ingestion.parsing import (
    BlockKind,
    DocumentBlock,
    NormalizedDocument,
    ParseError,
    ParseLimits,
    ParserRegistry,
)
from plummy.application.ingestion.pipeline import RunIngestionJob, RunOutcome
from plummy.application.storage import StorageArea, StorageKey
from plummy.domain.file_types import DocumentFormat
from plummy.infrastructure.clock import SystemClock
from plummy.infrastructure.db.engine import create_engine, create_session_factory
from plummy.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWorkFactory
from plummy.infrastructure.parsing.text import PlainTextParser
from plummy.infrastructure.storage.local import LocalFileStorage
from plummy.workers.app import INGESTION_QUEUE, INGESTION_TASK
from tests.integration.harness import Harness, running_auth_app
from tests.integration.rbac import CSRF_HEADER, active_user, audit_actions, login, setup_admin_client

pytestmark = pytest.mark.integration

FAKE_KEY = "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789WXYZ"  # fake test value; secret-scan: allow
URL = "/api/v1/documents"
DOCX_TYPES = (
    '<Types><Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'
)


@dataclass
class Env:
    harness: Harness
    admin: httpx.AsyncClient
    kb_id: str
    storage: LocalFileStorage


async def make_env(harness: Harness) -> Env:
    admin = await setup_admin_client(harness)
    provider = await admin.post(
        "/api/v1/providers",
        json={"name": "P", "provider_type": "openai", "base_url": "https://1.1.1.1/v1", "api_key": FAKE_KEY},
    )
    profile = await admin.post(
        "/api/v1/embedding-profiles",
        json={"name": "E", "provider_id": provider.json()["id"], "model_id": "m", "dimensions": 8},
    )
    kb = await admin.post(
        "/api/v1/knowledge-bases", json={"name": "KB", "embedding_profile_id": profile.json()["id"]}
    )
    assert kb.status_code == 201, kb.text
    return Env(harness, admin, kb.json()["id"], LocalFileStorage(harness.settings.storage_path))


@pytest.fixture
async def env(
    migrated_database_url: str, redis_url: str, clean_rate_limits: None, tmp_path: Path
) -> AsyncIterator[Env]:
    async with running_auth_app(migrated_database_url, redis_url, tmp_path / "bootstrap") as harness:
        yield await make_env(harness)


def docx_bytes(content_types: str = DOCX_TYPES) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("word/document.xml", "<w:document/>")
    return buffer.getvalue()


async def upload(
    client: httpx.AsyncClient, kb_id: str | None, filename: str, data: bytes, **extra: str
) -> httpx.Response:
    fields = {"knowledge_base_id": kb_id} if kb_id is not None else {}
    return await client.post(
        URL, data={**fields, **extra}, files={"file": (filename, data, "application/pdf")}
    )


def raw_multipart(parts: list[tuple[str, str | None, bytes]]) -> tuple[bytes, str]:
    boundary = "----plummy-test-boundary"
    body = b""
    for name, filename, data in parts:
        disposition = f'form-data; name="{name}"' + (f'; filename="{filename}"' if filename else "")
        body += f"--{boundary}\r\nContent-Disposition: {disposition}\r\n\r\n".encode() + data + b"\r\n"
    body += f"--{boundary}--\r\n".encode()
    return body, f"multipart/form-data; boundary={boundary}"


def files_in(storage: LocalFileStorage, area: StorageArea) -> list[Path]:
    root = storage.root / area.value
    return [path for path in root.rglob("*") if path.is_file()] if root.exists() else []


def problem(response: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body: dict[str, Any] = response.json()
    assert body["code"] == code, body
    return body


async def nothing_stored(env: Env) -> None:
    assert files_in(env.storage, StorageArea.QUARANTINE) == []
    assert files_in(env.storage, StorageArea.DOCUMENTS) == []
    assert await env.harness.sql("SELECT count(*) FROM documents") == [(0,)]
    assert await env.harness.sql("SELECT count(*) FROM ingestion_jobs") == [(0,)]


class StubOfficeParser:
    """Stands in for the isolated Docling parser (tested in tests/ingestion with the real parser stack).

    ``failures`` are raised one by one before it succeeds; it records how often it was called.
    """

    def __init__(self, *failures: Exception) -> None:
        self.failures, self.calls = list(failures), 0

    @property
    def name(self) -> str:
        return "stub-office/1"

    def supported_formats(self) -> frozenset[DocumentFormat]:
        return frozenset({DocumentFormat.PDF, DocumentFormat.DOCX, DocumentFormat.XLSX})

    async def parse(self, path: Path, file_format: DocumentFormat, limits: ParseLimits) -> NormalizedDocument:
        self.calls += 1
        if self.failures:
            raise self.failures.pop(0)
        return NormalizedDocument(
            blocks=(DocumentBlock(BlockKind.PARAGRAPH, "Office text", page=1),), page_count=1
        )


async def run(
    env: Env, job_id: str, storage: Any = None, office: StubOfficeParser | None = None
) -> RunOutcome:
    """What the worker task does, against the harness database and storage."""
    engine = create_engine(env.harness.settings, application_name="plummy-test-worker")
    uow = SqlAlchemyUnitOfWorkFactory(create_session_factory(engine))
    parsers = ParserRegistry([PlainTextParser(), office or StubOfficeParser()])
    try:
        return await RunIngestionJob(uow, storage or env.storage, SystemClock(), parsers).execute(
            UUID(job_id)
        )
    finally:
        await engine.dispose()


# --- intake -------------------------------------------------------------------------------------------------


async def test_upload_quarantines_the_file_and_queues_a_job(env: Env) -> None:
    data = "Отпуск переносится по заявлению.\n".encode()
    response = await upload(env.admin, env.kb_id, "../../etc/Policy.txt", data)
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["document"]["filename"] == "Policy.txt"
    assert body["document"]["processing_status"] == "pending"
    assert (body["job"]["type"], body["job"]["kind"], body["job"]["status"]) == (
        "ingestion",
        "ingest",
        "queued",
    )
    assert response.headers["cache-control"] == "no-store"

    [(external_id, mime, status)] = await env.harness.sql(
        "SELECT external_id, mime_type, processing_status FROM documents"
    )
    assert (external_id, mime, status) == (
        f"upload:{UUID(body['document']['id']).hex}",
        "text/plain",
        "pending",
    )
    [(checksum, size, key, version_status)] = await env.harness.sql(
        "SELECT checksum_sha256, size_bytes, storage_key, status FROM document_versions"
    )
    assert (bytes(checksum), size, version_status) == (hashlib.sha256(data).digest(), len(data), "pending")
    # stored under the server key only; the client's name never reaches the file system
    [stored] = files_in(env.storage, StorageArea.QUARANTINE)
    assert stored.name == key
    assert stored.read_bytes() == data
    [(job_key, task_id, requested_by)] = await env.harness.sql(
        "SELECT idempotency_key, celery_task_id, requested_by IS NOT NULL FROM ingestion_jobs"
    )
    assert job_key.startswith("ingest:")
    assert requested_by
    [message] = env.harness.queued_messages(INGESTION_QUEUE)
    assert message["headers"]["task"] == INGESTION_TASK
    assert message["headers"]["id"] == task_id
    assert message["headers"]["argsrepr"] == repr([body["job"]["id"]])
    assert "Policy" not in str(message)
    assert "document.uploaded" in await audit_actions(env.harness)


@pytest.mark.parametrize(
    ("data", "status", "code"),
    [
        (b"\x89PNG\r\n\x1a\n" + b"\x00" * 64, 415, "unsupported_media_type"),
        (b"MZ\x90\x00\x03", 415, "unsupported_media_type"),
        (b"", 422, "validation_error"),
    ],
)
async def test_rejected_at_intake_leaves_nothing(env: Env, data: bytes, status: int, code: str) -> None:
    problem(await upload(env.admin, env.kb_id, "fake.pdf", data), status, code)
    await nothing_stored(env)


async def test_size_limit_while_streaming(migrated_database_url: str, redis_url: str, tmp_path: Path) -> None:
    async with running_auth_app(
        migrated_database_url, redis_url, tmp_path / "bootstrap", upload_max_bytes=2048
    ) as harness:
        env = await make_env(harness)
        assert (await upload(env.admin, env.kb_id, "ok.txt", b"a" * 2048)).status_code == 202
        problem(await upload(env.admin, env.kb_id, "big.txt", b"a" * 2049), 413, "payload_too_large")
        # a declared length far above the limit is refused before the body is read
        body, content_type = raw_multipart(
            [("knowledge_base_id", None, env.kb_id.encode()), ("file", "x.txt", b"a")]
        )
        response = await env.admin.post(
            URL, content=body, headers={"content-type": content_type, "content-length": str(10**9)}
        )
        problem(response, 413, "payload_too_large")
        assert len(files_in(env.storage, StorageArea.QUARANTINE)) == 1
        assert await harness.sql("SELECT count(*) FROM documents") == [(1,)]


async def test_access_is_checked_before_the_file_is_stored(env: Env) -> None:
    member, member_id = await signed_in(env, "reader@example.com", ["member"])
    async with aclosing(member):
        problem(await upload(member, env.kb_id, "a.txt", b"text"), 404, "not_found")
        problem(await upload(member, "00000000-0000-7000-8000-000000000000", "a.txt", b"t"), 404, "not_found")
        await env.harness.sql(
            "INSERT INTO knowledge_base_access "
            "(id, knowledge_base_id, principal_type, user_id, access_level) "
            "VALUES (gen_random_uuid(), :kb, 'user', :user, 'read')",
            kb=env.kb_id,
            user=member_id,
        )
        problem(await upload(member, env.kb_id, "a.txt", b"text"), 403, "permission_denied")
        await nothing_stored(env)
        await env.harness.sql("UPDATE knowledge_base_access SET access_level = 'manage'")
        assert (await upload(member, env.kb_id, "a.txt", b"text")).status_code == 202
    async with env.harness.new_client() as anonymous:
        assert (await upload(anonymous, env.kb_id, "a.txt", b"text")).status_code in (401, 403)
    await env.harness.sql("UPDATE knowledge_bases SET deleted_at = now()")
    problem(await upload(env.admin, env.kb_id, "a.txt", b"text"), 404, "not_found")


async def signed_in(env: Env, email: str, roles: list[str]) -> tuple[httpx.AsyncClient, str]:
    user_id = await active_user(env.harness, env.admin, email, roles)
    client = env.harness.new_client()
    assert (await login(client, email)).status_code == 200
    return client, user_id


@pytest.mark.parametrize(
    ("parts", "error_code"),
    [
        ([("file", "a.txt", b"text"), ("knowledge_base_id", None, b"KB")], "invalid_uuid"),  # file first
        ([("knowledge_base_id", None, b"not-a-uuid"), ("file", "a.txt", b"t")], "invalid_uuid"),
        (
            [("knowledge_base_id", None, b"KB"), ("tenant", None, b"x"), ("file", "a.txt", b"t")],
            "unknown_field",
        ),
        ([("knowledge_base_id", None, b"KB")], "file_required"),
        (
            [("knowledge_base_id", None, b"KB"), ("file", "a.txt", b"t"), ("file", "b.txt", b"u")],
            "unexpected_part",
        ),
        ([("knowledge_base_id", None, b"KB" + b" " * 300), ("file", "a.txt", b"t")], "field_too_large"),
    ],
)
async def test_form_shape_is_strict(
    env: Env, parts: list[tuple[str, str | None, bytes]], error_code: str
) -> None:
    resolved = [(name, filename, data.replace(b"KB", env.kb_id.encode())) for name, filename, data in parts]
    body, content_type = raw_multipart(resolved)
    response = await env.admin.post(URL, content=body, headers={"content-type": content_type})
    found = problem(response, 422, "validation_error")
    assert found["errors"][0]["code"] == error_code
    await nothing_stored(env)


async def test_not_multipart_and_truncated_bodies(env: Env) -> None:
    found = problem(await env.admin.post(URL, json={"knowledge_base_id": env.kb_id}), 422, "validation_error")
    assert found["errors"][0]["code"] == "multipart_required"
    body, content_type = raw_multipart(
        [("knowledge_base_id", None, env.kb_id.encode()), ("file", "a.txt", b"x" * 100)]
    )
    response = await env.admin.post(URL, content=body[:-40], headers={"content-type": content_type})
    assert problem(response, 422, "validation_error")["errors"][0]["code"] == "malformed_multipart"
    await nothing_stored(env)


async def test_upload_rate_limit_per_user(env: Env) -> None:
    for _ in range(30):
        assert (await upload(env.admin, env.kb_id, "a.txt", b"t")).status_code == 202
    response = await upload(env.admin, env.kb_id, "a.txt", b"t")
    problem(response, 429, "rate_limited")
    assert int(response.headers["retry-after"]) >= 1
    assert len(files_in(env.storage, StorageArea.QUARANTINE)) == 30


async def test_csrf_token_is_required(env: Env) -> None:
    token = env.admin.headers.pop(CSRF_HEADER)
    try:
        assert (await upload(env.admin, env.kb_id, "a.txt", b"t")).status_code == 403
    finally:
        env.admin.headers[CSRF_HEADER] = token
    await nothing_stored(env)


# --- worker stages 2-7 --------------------------------------------------------------------------------------


async def uploaded(env: Env, filename: str, data: bytes) -> tuple[str, str]:
    response = await upload(env.admin, env.kb_id, filename, data)
    assert response.status_code == 202, response.text
    return response.json()["job"]["id"], response.json()["document"]["id"]


async def job_row(env: Env, job_id: str) -> tuple[Any, ...]:
    [row] = await env.harness.sql(
        "SELECT status, stage, result, attempt, error_code FROM ingestion_jobs WHERE id = :id", id=job_id
    )
    return tuple(row)


async def test_worker_validates_and_stores_the_original(env: Env) -> None:
    job_id, document_id = await uploaded(env, "contract.pdf", docx_bytes())  # a DOCX named .pdf
    assert await run(env, job_id) is RunOutcome.SUCCEEDED
    assert await job_row(env, job_id) == ("succeeded", "normalize", "processed", 1, None)
    assert files_in(env.storage, StorageArea.QUARANTINE) == []
    [(mime, version_mime, key, extracted, parser)] = await env.harness.sql(
        "SELECT d.mime_type, v.mime_type, v.storage_key, v.extracted_storage_key, v.parser FROM documents d "
        "JOIN document_versions v ON v.document_id = d.id WHERE d.id = :id",
        id=document_id,
    )
    docx = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    assert (mime, version_mime, parser) == (docx, docx, "stub-office/1")
    assert sorted(path.name for path in files_in(env.storage, StorageArea.DOCUMENTS)) == sorted(
        [key, extracted]
    )
    # a finished job is not run again (redelivery)
    assert await run(env, job_id) is RunOutcome.SKIPPED
    assert await job_row(env, job_id) == ("succeeded", "normalize", "processed", 1, None)


async def test_permanent_rejection_fails_the_document(env: Env) -> None:
    macro = docx_bytes(
        DOCX_TYPES.replace("wordprocessingml.document.main", "ms-word.document.macroEnabled.main")
    )
    job_id, document_id = await uploaded(env, "invoice.docm", macro)
    assert await run(env, job_id) is RunOutcome.FAILED
    assert await job_row(env, job_id) == ("failed", "validate_type", None, 1, "macro_enabled")
    [(doc_status, version_status, code)] = await env.harness.sql(
        "SELECT d.processing_status, v.status, v.error_code FROM documents d "
        "JOIN document_versions v ON v.document_id = d.id WHERE d.id = :id",
        id=document_id,
    )
    assert (doc_status, version_status, code) == ("failed", "failed", "macro_enabled")
    assert files_in(env.storage, StorageArea.QUARANTINE) == []
    assert files_in(env.storage, StorageArea.DOCUMENTS) == []


async def test_worker_rechecks_state_instead_of_trusting_the_message(env: Env) -> None:
    job_id, document_id = await uploaded(env, "a.txt", b"text")
    await env.harness.sql(
        "UPDATE documents SET deleted_at = now(), deletion_reason = 'user' WHERE id = :id", id=document_id
    )
    assert await run(env, job_id) is RunOutcome.CANCELLED
    assert await job_row(env, job_id) == ("cancelled", "authorize", None, 1, "resource_unavailable")
    assert files_in(env.storage, StorageArea.DOCUMENTS) == []

    other_job, _ = await uploaded(env, "b.txt", b"text")
    await env.harness.sql("UPDATE knowledge_bases SET deleted_at = now()")
    assert await run(env, other_job) is RunOutcome.CANCELLED
    assert await run(env, "00000000-0000-7000-8000-000000000000") is RunOutcome.SKIPPED


async def test_missing_original_fails_permanently(env: Env) -> None:
    job_id, _ = await uploaded(env, "a.txt", b"text")
    for path in files_in(env.storage, StorageArea.QUARANTINE):
        path.unlink()
    assert await run(env, job_id) is RunOutcome.FAILED
    assert (await job_row(env, job_id))[4] == "original_missing"


class FlakyStorage:
    """Real storage whose ``move`` fails a number of times (a full disk, an NFS hiccup)."""

    def __init__(self, inner: LocalFileStorage, failures: int) -> None:
        self._inner, self.failures = inner, failures

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    async def move(self, key: StorageKey, source: StorageArea, target: StorageArea) -> None:
        if self.failures > 0:
            self.failures -= 1
            raise OSError("disk full")
        await self._inner.move(key, source, target)


async def test_transient_errors_are_retried_then_dead(env: Env) -> None:
    job_id, _ = await uploaded(env, "a.txt", b"text")
    flaky = FlakyStorage(env.storage, failures=1)
    assert await run(env, job_id, flaky) is RunOutcome.RETRY
    assert await job_row(env, job_id) == ("queued", "store_original", None, 1, "transient_error")
    assert await run(env, job_id, flaky) is RunOutcome.SUCCEEDED
    assert await job_row(env, job_id) == ("succeeded", "normalize", "processed", 2, None)

    dead_job, dead_document = await uploaded(env, "b.txt", b"text")
    always = FlakyStorage(env.storage, failures=100)
    outcomes = [await run(env, dead_job, always) for _ in range(5)]
    assert outcomes == [RunOutcome.RETRY] * 4 + [RunOutcome.DEAD]
    assert await job_row(env, dead_job) == ("dead", "store_original", None, 5, "attempts_exhausted")
    assert await env.harness.sql(
        "SELECT processing_status FROM documents WHERE id = :id", id=dead_document
    ) == [("failed",)]
    assert await run(env, dead_job, always) is RunOutcome.SKIPPED


async def stored_json(env: Env, key: str) -> dict[str, Any]:
    [path] = [path for path in files_in(env.storage, StorageArea.DOCUMENTS) if path.name == key]
    data: dict[str, Any] = json.loads(path.read_bytes())
    return data


async def test_worker_parses_and_keeps_the_normalized_document(env: Env) -> None:
    markdown = "\n\n".join(["# Отпуск", "Отпуск 28 дней.", "## Перенос", "- по заявлению"]).encode()
    job_id, document_id = await uploaded(env, "policy.md", markdown)
    assert await run(env, job_id) is RunOutcome.SUCCEEDED
    [(version_status, extracted, parser, pages, doc_status)] = await env.harness.sql(
        "SELECT v.status, v.extracted_storage_key, v.parser, v.page_count, d.processing_status "
        "FROM documents d JOIN document_versions v ON v.document_id = d.id WHERE d.id = :id",
        id=document_id,
    )
    # chunking and embeddings are not wired yet: the version waits in processing, the document in pending
    assert (version_status, parser, pages, doc_status) == ("processing", "plummy-text/1", None, "pending")
    document = NormalizedDocument.from_json(await stored_json(env, extracted))
    assert [(block.kind, block.text, block.section_path) for block in document.blocks] == [
        (BlockKind.HEADING, "Отпуск", ("Отпуск",)),
        (BlockKind.PARAGRAPH, "Отпуск 28 дней.", ("Отпуск",)),
        (BlockKind.HEADING, "Перенос", ("Отпуск", "Перенос")),
        (BlockKind.LIST_ITEM, "- по заявлению", ("Отпуск", "Перенос")),
    ]


@pytest.mark.parametrize("code", ["parse_timeout", "page_limit_exceeded", "encrypted_file", "no_text"])
async def test_parse_errors_fail_the_document(env: Env, code: str) -> None:
    job_id, document_id = await uploaded(env, "report.docx", docx_bytes())
    office = StubOfficeParser(ParseError(code, "Parsing failed."))
    assert await run(env, job_id, office=office) is RunOutcome.FAILED
    assert await job_row(env, job_id) == ("failed", "parse", None, 1, code)
    [(doc_status, version_status, version_code, extracted)] = await env.harness.sql(
        "SELECT d.processing_status, v.status, v.error_code, v.extracted_storage_key FROM documents d "
        "JOIN document_versions v ON v.document_id = d.id WHERE d.id = :id",
        id=document_id,
    )
    assert (doc_status, version_status, version_code, extracted) == ("failed", "failed", code, None)
    assert len(files_in(env.storage, StorageArea.DOCUMENTS)) == 1  # the original only, no extracted JSON


async def test_empty_text_document_is_rejected(env: Env) -> None:
    job_id, _ = await uploaded(env, "blank.txt", b"\n   \n\t\n")
    assert await run(env, job_id) is RunOutcome.FAILED
    assert (await job_row(env, job_id))[1:] == ("parse", None, 1, "no_text")


async def test_transient_parser_error_is_retried_and_parsing_is_not_repeated(env: Env) -> None:
    job_id, document_id = await uploaded(env, "report.docx", docx_bytes())
    office = StubOfficeParser(OSError("scratch disk full"))
    assert await run(env, job_id, office=office) is RunOutcome.RETRY
    assert await job_row(env, job_id) == ("queued", "parse", None, 1, "transient_error")
    assert await run(env, job_id, office=office) is RunOutcome.SUCCEEDED
    assert office.calls == 2

    # a stored extraction is reused: a rerun of the stages does not call the parser again
    await env.harness.sql(
        "UPDATE ingestion_jobs SET status = 'queued', finished_at = NULL, result = NULL WHERE id = :id",
        id=job_id,
    )
    assert await run(env, job_id, office=office) is RunOutcome.SUCCEEDED
    assert office.calls == 2
    [(count,)] = await env.harness.sql(
        "SELECT count(*) FROM document_versions "
        "WHERE document_id = :id AND extracted_storage_key IS NOT NULL",
        id=document_id,
    )
    assert count == 1
    assert len(files_in(env.storage, StorageArea.DOCUMENTS)) == 2
