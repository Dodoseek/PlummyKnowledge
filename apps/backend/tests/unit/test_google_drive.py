"""The Google Drive connector against the fake Drive (ADR-020 §4, §5, §8, §9): parsing, browsing, listing,
changes, downloads and exports, size limits, retries and redirects, all through ``SafeHttpClient``."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest

from plummy.application.sources.ports import (
    BrowseKind,
    ConnectorAccessTokenRejectedError,
    ConnectorCursorInvalidError,
    ConnectorEgressDeniedError,
    ConnectorFileTooLargeError,
    ConnectorInvalidResponseError,
    ConnectorNotFoundError,
    ConnectorPermissionDeniedError,
    ConnectorQuotaExceededError,
    ConnectorRateLimitedError,
    ConnectorTransientError,
    RemoteItem,
    RemoteKind,
    SourceLocation,
)
from plummy.core.instance_config import build_egress_policy, parse_instance_config
from plummy.domain.secrets import SecretValue
from plummy.infrastructure.http.safe_client import SafeHttpClient
from plummy.infrastructure.integrations.sources import google_drive
from plummy.infrastructure.integrations.sources.google_common import GoogleEndpoints
from plummy.infrastructure.integrations.sources.google_drive import (
    DOCX,
    FOLDER_MIME,
    PDF,
    GoogleDriveConnector,
    _quote,
    parse_item,
)
from tests.fake_google import FakeGoogle, connector_config
from tests.fake_http import FakeHttpServer, Reply, serve

TOKEN = "access-test-token"
ACCESS = SecretValue(TOKEN)
LOCATION = SourceLocation(folder_id="folder-1")
DOC = "application/vnd.google-apps.document"


@dataclass
class Drive:
    google: FakeGoogle
    server: FakeHttpServer
    connector: GoogleDriveConnector
    sleeps: list[float]

    def api_requests(self) -> list[Any]:
        return [request for request in self.server.requests if request.target.startswith("/drive/")]


@asynccontextmanager
async def drive(*, max_attempts: int = 5) -> AsyncIterator[Drive]:
    google = FakeGoogle()
    google.access_tokens.add(TOKEN)
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async with serve(google.handle) as server:
        config = parse_instance_config(connector_config(server.port).encode())
        http = SafeHttpClient(build_egress_policy(config))
        connector = GoogleDriveConnector(
            http, GoogleEndpoints.from_config(config.google_drive), max_attempts=max_attempts, sleep=sleep
        )
        try:
            yield Drive(google, server, connector, sleeps)
        finally:
            await http.aclose()


async def _read(stream: AsyncIterator[bytes]) -> bytes:
    return b"".join([chunk async for chunk in stream])


# --- parsing --------------------------------------------------------------------------------------------


def _resource(**fields: Any) -> dict[str, Any]:
    return {"id": "f1", "name": "Report", "mimeType": "application/pdf", "parents": ["p1"], **fields}


def test_regular_file_revision_is_its_checksum() -> None:
    item = parse_item(
        _resource(md5Checksum="abc", version="7", size="12", modifiedTime="2026-11-26T10:00:00Z")
    )
    assert item.kind is RemoteKind.FILE
    assert item.revision == "md5:abc"
    assert item.size == 12
    assert item.filename == "Report"
    assert item.content_type == "application/pdf"
    assert item.modified_at is not None


@pytest.mark.parametrize(
    ("mime", "content_type", "filename"),
    [
        (DOC, DOCX, "Report.docx"),
        ("application/vnd.google-apps.spreadsheet", google_drive.XLSX, "Report.xlsx"),
        ("application/vnd.google-apps.presentation", PDF, "Report.pdf"),
    ],
)
def test_google_formats_are_exported(mime: str, content_type: str, filename: str) -> None:
    item = parse_item(_resource(mimeType=mime, version="7"))
    assert item.kind is RemoteKind.FILE
    assert (item.content_type, item.filename, item.revision) == (content_type, filename, "v:7")


def test_export_extension_is_not_doubled() -> None:
    assert parse_item(_resource(name="Plan.docx", mimeType=DOC)).filename == "Plan.docx"


@pytest.mark.parametrize(
    ("mime", "kind"),
    [
        (FOLDER_MIME, RemoteKind.FOLDER),
        ("application/vnd.google-apps.shortcut", RemoteKind.SHORTCUT),
        ("application/vnd.google-apps.form", RemoteKind.UNSUPPORTED),
        ("application/vnd.google-apps.site", RemoteKind.UNSUPPORTED),
    ],
)
def test_kinds(mime: str, kind: RemoteKind) -> None:
    assert parse_item(_resource(mimeType=mime)).kind is kind


@pytest.mark.parametrize(
    "resource",
    [
        [],
        _resource(id=""),
        _resource(id="x" * 257),
        _resource(id=5),
        _resource(name=None),
        _resource(parents="p1"),
        _resource(parents=[1]),
        _resource(trashed="no"),
        _resource(size="-1"),
        _resource(size=True),
        _resource(modifiedTime="yesterday"),
        _resource(modifiedTime="2026-11-26T10:00:00"),
        _resource(version=[1]),
    ],
)
def test_invalid_resources_are_rejected(resource: Any) -> None:
    with pytest.raises(ConnectorInvalidResponseError):
        parse_item(resource)


def test_long_names_are_cut_and_empty_names_get_a_placeholder() -> None:
    assert len(parse_item(_resource(name="n" * 5000)).name) == google_drive.MAX_NAME_LENGTH
    assert parse_item(_resource(name="")).name == "(untitled)"


def test_query_literals_are_escaped() -> None:
    assert _quote("a'b\\c") == "'a\\'b\\\\c'"


# --- browsing and listing -------------------------------------------------------------------------------


async def test_top_level_shows_shared_drives_own_and_shared_folders() -> None:
    async with drive() as env:
        google = env.google
        google.shared_drives.append({"id": "drive1", "name": "Team"})
        google.folder("Mine")
        google.folder("Shared with me", parent="someone-elses-folder", shared=True)
        google.add("a file at the root", content=b"x")
        hidden = google.folder("Trashed")
        google.files[hidden]["trashed"] = True
        page = await env.connector.browse(ACCESS, None, page_token=None)
    assert [(entry.name, entry.kind) for entry in page.entries] == [
        ("Team", BrowseKind.SHARED_DRIVE),
        ("Mine", BrowseKind.FOLDER),
        ("Shared with me", BrowseKind.FOLDER),
    ]
    assert page.next_page_token is None


async def test_browse_under_a_folder_lists_subfolders_only() -> None:
    async with drive() as env:
        parent = env.google.folder("Sales")
        env.google.folder("2026", parent=parent)
        env.google.add("notes.txt", parent=parent, content=b"x")
        page = await env.connector.browse(ACCESS, parent, page_token=None)
        assert [entry.name for entry in page.entries] == ["2026"]
        requests = env.api_requests()
    assert all("drives" not in request.target for request in requests)  # shared drives only at the top


async def test_listing_pages_through_children(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(google_drive, "LIST_PAGE_SIZE", 2)
    async with drive() as env:
        folder = env.google.folder("Sales")
        for name in ("a", "b", "c"):
            env.google.add(f"{name}.txt", parent=folder, content=name.encode())
        trashed = env.google.add("old.txt", parent=folder, content=b"x")
        env.google.files[trashed]["trashed"] = True
        first = await env.connector.list_children(ACCESS, folder, page_token=None)
        assert first.next_page_token is not None
        second = await env.connector.list_children(ACCESS, folder, page_token=first.next_page_token)
    assert [item.name for item in first.items + second.items] == ["a.txt", "b.txt", "c.txt"]
    assert second.next_page_token is None


async def test_get_item_and_the_root_of_a_shared_drive() -> None:
    async with drive() as env:
        env.google.shared_drives.append({"id": "drive1", "name": "Team"})
        file_id = env.google.add("x.txt", content=b"abc")
        item = await env.connector.get_item(ACCESS, file_id)
        root = await env.connector.get_item(ACCESS, "drive1")
        with pytest.raises(ConnectorNotFoundError):
            await env.connector.get_item(ACCESS, "missing")
    assert (item.kind, item.size) == (RemoteKind.FILE, 3)
    assert item.revision is not None
    assert item.revision.startswith("md5:")
    assert (root.kind, root.drive_id, root.name) == (RemoteKind.FOLDER, "drive1", "Team")


async def test_the_token_goes_only_into_the_authorization_header() -> None:
    async with drive() as env:
        folder = env.google.folder("Sales")
        await env.connector.list_children(ACCESS, folder, page_token=None)
        [request] = env.api_requests()
    assert request.headers["authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in request.target


# --- changes ----------------------------------------------------------------------------------------------


async def test_changes_page_until_finished(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(google_drive, "LIST_PAGE_SIZE", 2)
    async with drive() as env:
        google = env.google
        cursor = await env.connector.start_cursor(ACCESS, LOCATION)
        kept = google.add("kept.txt", content=b"x")
        gone = google.add("gone.txt", content=b"y")
        trashed = google.add("trashed.txt", content=b"z")
        google.files[trashed]["trashed"] = True
        google.change(kept)
        google.change(gone, removed=True)
        google.change(trashed)
        first = await env.connector.get_changes(ACCESS, LOCATION, cursor)
        second = await env.connector.get_changes(ACCESS, LOCATION, first.cursor)
        idle = await env.connector.get_changes(ACCESS, LOCATION, second.cursor)
    assert not first.finished
    assert second.finished
    changes = first.changes + second.changes
    assert [(change.item_id, change.removed) for change in changes] == [
        (kept, False),
        (gone, True),
        (trashed, False),
    ]
    assert changes[2].item is not None
    assert changes[2].item.trashed
    assert (idle.changes, idle.finished, idle.cursor) == ((), True, second.cursor)


async def test_an_unknown_cursor_needs_a_reconciliation() -> None:
    async with drive() as env:
        with pytest.raises(ConnectorCursorInvalidError):
            await env.connector.get_changes(ACCESS, LOCATION, "999")


# --- downloads ----------------------------------------------------------------------------------------------


def _item(env: Drive, item_id: str) -> RemoteItem:
    return parse_item(env.google._public(env.google.files[item_id]))


async def test_download_of_a_regular_file_and_an_export() -> None:
    async with drive() as env:
        file_id = env.google.add("x.bin", mime="application/octet-stream", content=b"\x00binary")
        doc_id = env.google.add("Plan", mime=DOC, content=b"PK-docx")
        assert (
            await _read(env.connector.download(ACCESS, _item(env, file_id), max_bytes=100)) == b"\x00binary"
        )
        assert await _read(env.connector.download(ACCESS, _item(env, doc_id), max_bytes=100)) == b"PK-docx"
        export = env.api_requests()[-1]
    assert f"/files/{doc_id}/export" in export.target
    assert "mimeType=application%2Fvnd.openxmlformats" in export.target


async def test_size_limits() -> None:
    async with drive() as env:
        file_id = env.google.add("big.bin", mime="application/octet-stream", content=b"x" * 50)
        doc_id = env.google.add("Big", mime=DOC, content=b"x" * 50)  # an export has no known size
        with pytest.raises(ConnectorFileTooLargeError):
            await _read(env.connector.download(ACCESS, _item(env, file_id), max_bytes=10))
        assert env.api_requests() == []  # refused before downloading
        with pytest.raises(ConnectorFileTooLargeError):
            await _read(env.connector.download(ACCESS, _item(env, doc_id), max_bytes=10))


async def test_folders_are_not_downloaded() -> None:
    async with drive() as env:
        folder = env.google.folder("Sales")
        with pytest.raises(ValueError, match="only files"):
            await _read(env.connector.download(ACCESS, _item(env, folder), max_bytes=10))


async def test_a_download_redirect_outside_the_connector_hosts_is_refused() -> None:
    async with drive() as env:
        env.google.download_redirect = "https://elsewhere.example"
        file_id = env.google.add("x.bin", mime="application/octet-stream", content=b"abc")
        with pytest.raises(ConnectorEgressDeniedError):
            await _read(env.connector.download(ACCESS, _item(env, file_id), max_bytes=100))


async def test_export_limit_of_google() -> None:
    async with drive() as env:
        doc_id = env.google.add("Huge", mime=DOC, content=b"x")
        env.google.api_failures.append(
            Reply.json({"error": {"errors": [{"reason": "exportSizeLimitExceeded"}]}}, status=403)
        )
        with pytest.raises(ConnectorFileTooLargeError):
            await _read(env.connector.download(ACCESS, _item(env, doc_id), max_bytes=100))


# --- errors and retries -------------------------------------------------------------------------------------


def _reason(reason: str, status: int = 403, **headers: str) -> Reply:
    return Reply.json({"error": {"errors": [{"reason": reason}]}}, status=status, **headers)


async def test_rate_limits_and_server_errors_are_retried() -> None:
    async with drive() as env:
        env.google.api_failures += [
            Reply.json({}, status=429, **{"Retry-After": "7"}),
            _reason("userRateLimitExceeded"),
            Reply.json({}, status=503),
        ]
        folder = env.google.folder("Sales")
        page = await env.connector.list_children(ACCESS, folder, page_token=None)
    assert page.items == ()
    assert env.sleeps[0] == 7.0  # Retry-After first
    assert len(env.sleeps) == 3


async def test_retries_stop_after_max_attempts() -> None:
    async with drive(max_attempts=2) as env:
        env.google.api_failures += [Reply.json({}, status=429), Reply.json({}, status=429)]
        with pytest.raises(ConnectorRateLimitedError):
            await env.connector.start_cursor(ACCESS, LOCATION)
    assert len(env.sleeps) == 1


async def test_downloads_are_retried_before_the_first_byte() -> None:
    async with drive() as env:
        file_id = env.google.add("x.bin", mime="application/octet-stream", content=b"abc")
        env.google.api_failures.append(Reply.json({}, status=500))
        assert await _read(env.connector.download(ACCESS, _item(env, file_id), max_bytes=100)) == b"abc"
    assert len(env.sleeps) == 1


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        (Reply.json({}, status=401), ConnectorAccessTokenRejectedError),
        (_reason("dailyLimitExceeded"), ConnectorQuotaExceededError),
        (_reason("insufficientFilePermissions"), ConnectorPermissionDeniedError),
        (Reply.json({}, status=404), ConnectorNotFoundError),
        (Reply(200, {"Content-Type": "application/json"}, b"[]"), ConnectorInvalidResponseError),
        (Reply.json({"startPageToken": 5}), ConnectorInvalidResponseError),
    ],
)
async def test_errors_that_are_not_retried(reply: Reply, error: type[Exception]) -> None:
    async with drive() as env:
        env.google.api_failures.append(reply)
        with pytest.raises(error):
            await env.connector.start_cursor(ACCESS, LOCATION)
    assert env.sleeps == []


async def test_network_failure_is_transient() -> None:
    http = SafeHttpClient(
        build_egress_policy(parse_instance_config(connector_config(9).encode()))  # nothing listens on :9
    )
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    endpoints = GoogleEndpoints.from_config(parse_instance_config(connector_config(9).encode()).google_drive)
    connector = GoogleDriveConnector(http, endpoints, max_attempts=2, sleep=sleep)
    with pytest.raises(ConnectorTransientError):
        await connector.start_cursor(ACCESS, LOCATION)
    await http.aclose()
    assert len(sleeps) == 1


async def test_an_endpoint_without_an_allowlist_entry_is_refused() -> None:
    http = SafeHttpClient(build_egress_policy(parse_instance_config(b"")))
    connector = GoogleDriveConnector(http, GoogleEndpoints(api_url="http://127.0.0.1:9"))
    with pytest.raises(ConnectorEgressDeniedError):
        await connector.start_cursor(ACCESS, LOCATION)
    await http.aclose()


def test_at_least_one_attempt() -> None:
    with pytest.raises(ValueError, match="max_attempts"):
        GoogleDriveConnector(SafeHttpClient(build_egress_policy(parse_instance_config(b""))), max_attempts=0)
