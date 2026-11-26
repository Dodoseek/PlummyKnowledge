"""Deletions after a backup, exported from the source and reapplied on a restored instance (ADR-023 §6).

Real PostgreSQL and Redis. The export runs on deletions made through the API; the reapply runs on live
objects (as they are in a restored database) and must delete them with the API's own deletion code, as a
system action, idempotently. The end-to-end restore (dump, restore, reapply) is the CI restore drill.
"""

from __future__ import annotations

import asyncio
import io
import json
import stat
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest

from plummy.application.maintenance.deletions import (
    Deletion,
    DeletionExport,
    ExportDeletions,
    ReapplyDeletions,
)
from plummy.cli.app import main
from plummy.infrastructure.clock import SystemClock
from plummy.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWorkFactory
from plummy.workers.app import MAINTENANCE_QUEUE, PURGE_TASK
from tests.integration.harness import Harness, running_auth_app
from tests.integration.rbac import ADMIN_PASSWORD, login
from tests.integration.test_assistants_api import Env, create, make_env, signed_in

pytestmark = pytest.mark.integration


@dataclass
class World:
    env: Env
    document: str
    conversation: str
    member_id: str
    member_email: str
    source: str
    disconnected_source: str


@pytest.fixture
async def env(
    migrated_database_url: str, redis_url: str, clean_rate_limits: None, tmp_path: Path
) -> AsyncIterator[Env]:
    async with running_auth_app(migrated_database_url, redis_url, tmp_path / "bootstrap") as harness:
        yield await make_env(harness)


def uow(harness: Harness) -> SqlAlchemyUnitOfWorkFactory:
    return SqlAlchemyUnitOfWorkFactory(harness.resources.session_factory)


def reapplier(harness: Harness) -> ReapplyDeletions:
    resources = harness.resources
    return ReapplyDeletions(uow(harness), resources.jobs, SystemClock(), resources.secrets, {})


async def upload(env: Env, kb_id: str, name: str = "policy.md") -> str:
    response = await env.admin.post(
        "/api/v1/documents",
        data={"knowledge_base_id": kb_id},
        files={"file": (name, b"# Policy\n\nVacation is filed two weeks ahead.\n", "text/markdown")},
    )
    assert response.status_code in (201, 202), response.text
    document_id: str = response.json()["document"]["id"]
    return document_id


async def external_source(env: Env, kb_id: str) -> str:
    source_id = str(uuid4())
    await env.harness.sql(
        "INSERT INTO data_sources (id, type, name, status) VALUES (:id, 'google_drive', 'Drive', 'active')",
        id=source_id,
    )
    await env.harness.sql(
        "INSERT INTO knowledge_base_sources (knowledge_base_id, data_source_id) VALUES (:kb, :source)",
        kb=kb_id,
        source=source_id,
    )
    return source_id


async def world(env: Env) -> World:
    """Live objects, as a restored database has them."""
    assistant = await create(env, name="HR", knowledge_base_ids=[env.hr])
    member, member_id = await signed_in(env, "member@example.com", ["member"])
    grant = {"principal_type": "user", "principal_id": member_id, "access_level": "read"}
    response = await env.admin.put(f"/api/v1/knowledge-bases/{env.hr}/access", json={"grants": [grant]})
    assert response.status_code == 200, response.text
    conversation = await member.post("/api/v1/conversations", json={"assistant_id": assistant["id"]})
    assert conversation.status_code == 201, conversation.text
    await member.aclose()
    return World(
        env,
        document=await upload(env, env.hr),
        conversation=conversation.json()["id"],
        member_id=member_id,
        member_email="member@example.com",
        source=await external_source(env, env.hr),
        disconnected_source=await external_source(env, env.hr),
    )


def export_of(*items: tuple[str, str]) -> DeletionExport:
    now = datetime.now(UTC)
    return DeletionExport(
        since=now - timedelta(hours=1),
        exported_at=now,
        deletions=[Deletion(action, UUID(target), now) for action, target in items],
        not_reapplied={},
    )


async def system_audit(harness: Harness) -> list[tuple[str, str, str]]:
    rows = await harness.sql(
        "SELECT action, target_type, details->>'deletion' FROM audit_logs "
        "WHERE actor_type = 'system' AND action = 'maintenance.deletion_reapplied' ORDER BY created_at"
    )
    return [(row[0], row[1], row[2]) for row in rows]


# --- export -------------------------------------------------------------------------------------------------


async def test_export_lists_the_deletions_after_since_in_order(env: Env) -> None:
    before = await upload(env, env.hr, "before.md")
    assert (await env.admin.delete(f"/api/v1/documents/{before}")).status_code == 204
    since = datetime.now(UTC)

    after = await upload(env, env.hr, "after.md")
    assert (await env.admin.delete(f"/api/v1/documents/{after}")).status_code == 204
    assert (await env.admin.delete(f"/api/v1/knowledge-bases/{env.it}")).status_code in (200, 204)
    _, member_id = await signed_in(env, "member@example.com", ["member"])
    _, other_id = await signed_in(env, "other@example.com", ["member"])
    disabled = await env.admin.patch(f"/api/v1/users/{other_id}", json={"status": "disabled"})
    assert disabled.status_code == 200, disabled.text
    assert (await env.admin.delete(f"/api/v1/users/{member_id}")).status_code == 204

    export = await ExportDeletions(uow(env.harness), SystemClock()).execute(since)

    assert [(item.action, str(item.target_id)) for item in export.deletions] == [
        ("document.deleted", after),
        ("kb.deleted", env.it),
        ("user.deleted", member_id),
    ]
    assert export.not_reapplied.get("user.disabled") == 1
    # identifiers only: no names, emails or content in the file
    text = export.to_json()
    assert "example.com" not in text
    assert "Vacation" not in text
    assert "HR" not in text
    assert DeletionExport.from_json(text) == export
    rows = await env.harness.sql(
        "SELECT actor_type, details->>'deletions' FROM audit_logs "
        "WHERE action = 'maintenance.deletions_exported'"
    )
    assert [(row[0], row[1]) for row in rows] == [("system", "3")]


async def test_export_ignores_refused_attempts(env: Env) -> None:
    since = datetime.now(UTC)
    member, _ = await signed_in(env, "member@example.com", ["member"])
    document = await upload(env, env.hr)
    refused = await member.delete(f"/api/v1/documents/{document}")
    assert refused.status_code in (403, 404)
    await member.aclose()
    export = await ExportDeletions(uow(env.harness), SystemClock()).execute(since)
    assert export.deletions == []


async def test_export_needs_an_aware_time(env: Env) -> None:
    with pytest.raises(ValueError, match="timezone"):
        await ExportDeletions(uow(env.harness), SystemClock()).execute(datetime(2026, 1, 1))


# --- reapply ------------------------------------------------------------------------------------------------


async def test_reapply_deletes_with_the_api_code_and_is_idempotent(env: Env) -> None:
    w = await world(env)
    export = export_of(
        ("document.deleted", w.document),
        ("conversation.deleted", w.conversation),
        ("data_source.disconnected", w.disconnected_source),
        ("data_source.deleted", w.source),
        ("user.deleted", w.member_id),
        ("kb.deleted", env.it),
    )

    report = await reapplier(env.harness).execute(export)

    assert sum(report.applied.values()) == 6
    assert not report.skipped
    assert not report.refused
    # the document left the knowledge base at once and its purge was published
    assert (await env.admin.get(f"/api/v1/documents/{w.document}")).status_code == 404
    purges = [m for m in env.harness.queued_messages(MAINTENANCE_QUEUE) if m["headers"]["task"] == PURGE_TASK]
    assert any(w.document in message["headers"]["argsrepr"] for message in purges)
    assert (await env.admin.get(f"/api/v1/knowledge-bases/{env.it}")).status_code == 404
    rows = await env.harness.sql(
        "SELECT id::text, status, deleted_at IS NOT NULL FROM data_sources WHERE id IN (:a, :b)",
        a=w.source,
        b=w.disconnected_source,
    )
    states = {row[0]: (row[1], row[2]) for row in rows}
    assert states[w.disconnected_source] == ("disconnected", False)
    assert states[w.source][1] is True
    hidden = await env.harness.sql(
        "SELECT deleted_at IS NOT NULL FROM conversations WHERE id = :id", id=w.conversation
    )
    assert hidden == [(True,)]
    async with env.harness.new_client() as client:
        assert (await login(client, w.member_email, ADMIN_PASSWORD)).status_code == 401
    audit = await system_audit(env.harness)
    assert sorted(entry[2] for entry in audit) == sorted(
        [
            "document.deleted",
            "conversation.deleted",
            "data_source.disconnected",
            "data_source.deleted",
            "user.deleted",
            "kb.deleted",
        ]
    )

    again = await reapplier(env.harness).execute(export)
    assert not again.applied
    assert sum(again.skipped.values()) == 6
    assert len(await system_audit(env.harness)) == 6  # nothing recorded for skipped items


async def test_reapply_skips_what_does_not_exist(env: Env) -> None:
    missing = str(uuid4())
    export = export_of(*[(action, missing) for action in ("document.deleted", "kb.deleted", "user.deleted")])
    report = await reapplier(env.harness).execute(export)
    assert not report.applied
    assert sum(report.skipped.values()) == 3


async def test_reapply_never_deletes_the_last_administrator(env: Env) -> None:
    rows = await env.harness.sql("SELECT id::text FROM users WHERE deleted_at IS NULL")
    (admin_id,) = [row[0] for row in rows]
    report = await reapplier(env.harness).execute(export_of(("user.deleted", admin_id)))
    assert report.refused == {"user.deleted": 1}
    me = await env.admin.get("/api/v1/auth/me")
    assert me.status_code == 200
    assert await system_audit(env.harness) == []


async def test_a_deleted_document_is_not_found_after_reapply(env: Env) -> None:
    """The point of reapplying: a document deleted after the backup does not come back in search."""
    w = await world(env)
    await reapplier(env.harness).execute(export_of(("document.deleted", w.document)))
    listing = await env.admin.get("/api/v1/documents", params={"knowledge_base_id": env.hr})
    assert listing.status_code == 200, listing.text
    assert w.document not in json.dumps(listing.json())


async def test_member_cannot_reach_anything_through_maintenance(env: Env) -> None:
    """Maintenance runs only from the server-side CLI: there is no API route for it."""
    member, _ = await signed_in(env, "member@example.com", ["member"])
    for path in ("/api/v1/maintenance/reapply-deletions", "/api/v1/maintenance/export-deletions"):
        response: httpx.Response = await member.post(path, json={})
        assert response.status_code == 404
    await member.aclose()


# --- CLI ----------------------------------------------------------------------------------------------------


def cli(harness: Harness, *argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), settings=harness.settings, stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


async def test_cli_export_then_reapply(env: Env, tmp_path: Path) -> None:
    since = datetime.now(UTC).isoformat()
    document = await upload(env, env.hr)
    assert (await env.admin.delete(f"/api/v1/documents/{document}")).status_code == 204
    output = tmp_path / "deletions.json"

    code, _, err = await asyncio.to_thread(
        cli, env.harness, "maintenance", "export-deletions", "--since", since, "--output", str(output)
    )
    assert code == 0, err
    assert "document.deleted: 1" in err
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert DeletionExport.from_json(output.read_text()).deletions[0].target_id == UUID(document)

    # never overwritten
    code, _, err = await asyncio.to_thread(
        cli, env.harness, "maintenance", "export-deletions", "--since", since, "--output", str(output)
    )
    assert code == 1
    assert "exists" in err

    # already deleted here: reapplying changes nothing and says so
    code, _, err = await asyncio.to_thread(
        cli, env.harness, "maintenance", "reapply-deletions", "--input", str(output)
    )
    assert code == 0, err
    assert "applied: 0" in err
    assert "already deleted or absent: 1" in err


async def test_cli_rejects_a_bad_file_without_echoing_it(env: Env, tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text('{"format": "something-else", "secret": "do-not-print-me"}')
    code, out, err = await asyncio.to_thread(
        cli, env.harness, "maintenance", "reapply-deletions", "--input", str(bad)
    )
    assert code == 1
    assert "plummy-deletions/1" in err
    assert "do-not-print-me" not in err + out


def test_cli_needs_a_time_with_an_offset(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(
            [
                "maintenance",
                "export-deletions",
                "--since",
                "2026-11-26T03:00:00",
                "--output",
                str(tmp_path / "x"),
            ]
        )
    assert exit_info.value.code == 2
