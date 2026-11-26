"""The Microsoft Graph connector (ADR-021 §4-§7) against a fake Graph on loopback, through the real
``SafeHttpClient`` and network policy: picker levels, opaque identifiers, listing, delta, downloads and the
mapping of Graph answers to typed errors."""

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
    ConnectorRateLimitedError,
    RemoteKind,
    SourceLocation,
)
from plummy.core.instance_config import build_egress_policy, parse_instance_config
from plummy.domain.secrets import SecretValue
from plummy.infrastructure.http.safe_client import SafeHttpClient
from plummy.infrastructure.integrations.sources import microsoft_graph
from plummy.infrastructure.integrations.sources.microsoft_common import MicrosoftEndpoints
from plummy.infrastructure.integrations.sources.microsoft_graph import (
    MicrosoftGraphConnector,
    item_ref,
    parse_item,
    parse_ref,
)
from tests.fake_http import FakeHttpServer, Reply, serve
from tests.fake_microsoft import TENANT, FakeMicrosoft, connector_config

TOKEN = "eyJ0eXAi.access-test-token"
ACCESS = SecretValue(TOKEN)
DRIVE = "b!AbC-dEf_123"


@dataclass
class Graph:
    microsoft: FakeMicrosoft
    server: FakeHttpServer
    connector: MicrosoftGraphConnector
    sleeps: list[float]

    def api_requests(self) -> list[Any]:
        return [request for request in self.server.requests if request.target.startswith("/v1.0/")]


@asynccontextmanager
async def graph(*, max_attempts: int = 5) -> AsyncIterator[Graph]:
    microsoft = FakeMicrosoft()
    microsoft.access_tokens.add(TOKEN)
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async with serve(microsoft.handle) as server:
        config = parse_instance_config(connector_config(server.port).encode())
        http = SafeHttpClient(build_egress_policy(config))
        endpoints = MicrosoftEndpoints.for_tenant(TENANT, config.microsoft_365)
        connector = MicrosoftGraphConnector(http, endpoints, max_attempts=max_attempts, sleep=sleep)
        try:
            yield Graph(microsoft, server, connector, sleeps)
        finally:
            await http.aclose()


async def _read(stream: AsyncIterator[bytes]) -> bytes:
    return b"".join([chunk async for chunk in stream])


# --- identifiers and parsing ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "parsed"),
    [
        ("site:contoso.sharepoint.com,1a2b,3c4d", ("site", "contoso.sharepoint.com,1a2b,3c4d", None)),
        (f"drive:{DRIVE}", ("drive", DRIVE, None)),
        (f"item:{DRIVE}:01ABCDEF", ("item", DRIVE, "01ABCDEF")),
        (f"item:{DRIVE}:D4648F06C91D9D3D!54927", ("item", DRIVE, "D4648F06C91D9D3D!54927")),
    ],
)
def test_identifiers_round_trip(value: str, parsed: tuple[str, str, str | None]) -> None:
    assert parse_ref(value) == parsed


@pytest.mark.parametrize(
    "value",
    [
        "",
        "folder-1",
        "drive:",
        "site:",
        f"item:{DRIVE}",
        f"item:{DRIVE}:",
        "item::01ABC",
        "drive:../../me",
        "site:a/b",
        f"item:{DRIVE}:01AB/children",
        f"item:{DRIVE}:01AB:extra",
        "user:01ABC",
        "drive:" + "a" * 300,
    ],
)
def test_foreign_identifiers_are_not_found(value: str) -> None:
    with pytest.raises(ConnectorNotFoundError):
        parse_ref(value)


def _raw(**fields: Any) -> dict[str, Any]:
    return {
        "id": "01FILE",
        "name": "Contract.docx",
        "parentReference": {"driveId": DRIVE, "id": "01PARENT"},
        "lastModifiedDateTime": "2026-11-26T10:00:00Z",
        **fields,
    }


def test_a_file_carries_the_drive_in_its_identifiers() -> None:
    item = parse_item(
        _raw(file={"mimeType": "text/plain", "hashes": {"quickXorHash": "qx=="}}, size=12), DRIVE
    )
    assert item.id == item_ref(DRIVE, "01FILE")
    assert item.parents == (item_ref(DRIVE, "01PARENT"),)
    assert (item.kind, item.mime_type, item.size, item.drive_id) == (RemoteKind.FILE, "text/plain", 12, DRIVE)
    assert item.revision == "qx:qx=="
    assert item.modified_at is not None
    assert item.modified_at.tzinfo is not None


@pytest.mark.parametrize(
    ("file", "ctag", "revision"),
    [
        ({"hashes": {"quickXorHash": "q", "sha256Hash": "s", "sha1Hash": "h"}}, '"c:1"', "qx:q"),
        ({"hashes": {"sha256Hash": "s", "sha1Hash": "h"}}, '"c:1"', "sha256:s"),
        ({"hashes": {"sha1Hash": "h"}}, '"c:1"', "sha1:h"),
        ({}, '"c:1"', 'ctag:"c:1"'),
        ({}, None, None),
    ],
)
def test_revision_is_a_content_hash_then_ctag_never_etag(
    file: dict[str, Any], ctag: str | None, revision: str | None
) -> None:
    raw = _raw(file=file, eTag='"e:changes-on-rename"')
    if ctag is not None:
        raw["cTag"] = ctag
    assert parse_item(raw, DRIVE).revision == revision


@pytest.mark.parametrize(
    ("facets", "kind"),
    [
        ({"folder": {"childCount": 2}}, RemoteKind.FOLDER),
        ({"root": {}, "folder": {}}, RemoteKind.FOLDER),
        ({"remoteItem": {"id": "x"}, "folder": None}, RemoteKind.SHORTCUT),
        ({"package": {"type": "oneNote"}}, RemoteKind.UNSUPPORTED),
        ({}, RemoteKind.UNSUPPORTED),
    ],
)
def test_kinds(facets: dict[str, Any], kind: RemoteKind) -> None:
    item = parse_item(_raw(**facets), DRIVE)
    assert item.kind is kind
    if kind is not RemoteKind.FILE:
        assert (item.size, item.revision) == (None, None)


def test_a_file_without_mime_type_is_octet_stream_and_names_are_bounded() -> None:
    item = parse_item(_raw(name="n" * 5000, file={}), DRIVE)
    assert item.mime_type == "application/octet-stream"
    assert len(item.name) == microsoft_graph.MAX_NAME_LENGTH
    assert parse_item(_raw(name="", file={}), DRIVE).name == "(untitled)"


def test_a_root_has_no_parent() -> None:
    item = parse_item({"id": "01ROOT", "name": "root", "root": {}, "folder": {}}, DRIVE)
    assert item.parents == ()


@pytest.mark.parametrize(
    "raw",
    [
        "not an object",
        _raw(id="01/../x"),
        _raw(id=5),
        _raw(parentReference={"driveId": "b!other", "id": "01P"}),  # an item of another drive
        _raw(parentReference={"driveId": DRIVE, "id": "../01P"}),
        _raw(name=None),
        _raw(file={"hashes": "x"}),
        _raw(file={}, size=-1),
        _raw(file={}, size="12"),
        _raw(lastModifiedDateTime="2026-11-26T10:00:00"),  # no time zone
        _raw(folder="yes"),
    ],
)
def test_malformed_items_are_refused(raw: Any) -> None:
    with pytest.raises(ConnectorInvalidResponseError):
        parse_item(raw, DRIVE)


# --- the picker -----------------------------------------------------------------------------------------


async def test_the_top_level_shows_the_own_onedrive_and_the_sites() -> None:
    async with graph() as env:
        own = env.microsoft.drive("OneDrive", own=True)
        legal = env.microsoft.site("Legal")
        env.microsoft.site("Marketing")
        page = await env.connector.browse(ACCESS, None, page_token=None)
        assert [(entry.id, entry.name, entry.kind) for entry in page.entries] == [
            (f"drive:{own}", "OneDrive", BrowseKind.DRIVE),
            (f"site:{legal}", "Legal", BrowseKind.SITE),
            (page.entries[2].id, "Marketing", BrowseKind.SITE),
        ]
        assert page.next_page_token is None


async def test_without_a_onedrive_only_sites_are_shown() -> None:
    async with graph() as env:
        env.microsoft.site("Legal")
        page = await env.connector.browse(ACCESS, None, page_token=None)
        assert [entry.kind for entry in page.entries] == [BrowseKind.SITE]


async def test_at_most_the_first_sites_are_shown(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(microsoft_graph, "MAX_SITES", 2)
    async with graph() as env:
        for name in ("A", "B", "C"):
            env.microsoft.site(name)
        page = await env.connector.browse(ACCESS, None, page_token=None)
        assert [entry.name for entry in page.entries] == ["A", "B"]


async def test_a_site_lists_its_libraries_and_a_library_its_folders() -> None:
    async with graph() as env:
        site = env.microsoft.site("Legal")
        library = env.microsoft.drive("Documents", site=site)
        env.microsoft.drive("Elsewhere", site=env.microsoft.site("Other"))
        libraries = await env.connector.browse(ACCESS, f"site:{site}", page_token=None)
        assert [(entry.id, entry.name, entry.kind) for entry in libraries.entries] == [
            (f"drive:{library}", "Documents", BrowseKind.DRIVE)
        ]
        contracts = env.microsoft.folder(library, "Contracts")
        env.microsoft.add(library, "readme.txt", content=b"x")  # files are not shown in the picker
        nested = env.microsoft.folder(library, "2026", parent=contracts)
        top = await env.connector.browse(ACCESS, f"drive:{library}", page_token=None)
        assert [(entry.id, entry.name, entry.kind) for entry in top.entries] == [
            (item_ref(library, contracts), "Contracts", BrowseKind.FOLDER)
        ]
        inner = await env.connector.browse(ACCESS, item_ref(library, contracts), page_token=None)
        assert [entry.id for entry in inner.entries] == [item_ref(library, nested)]


async def test_picker_pages_carry_only_the_skiptoken() -> None:
    async with graph() as env:
        library = env.microsoft.drive("Documents", site=env.microsoft.site("Legal"))
        for name in ("a", "b", "c"):
            env.microsoft.folder(library, name)
        env.microsoft.page_size = 2
        first = await env.connector.browse(ACCESS, f"drive:{library}", page_token=None)
        assert first.next_page_token == "$skiptoken=2"
        second = await env.connector.browse(ACCESS, f"drive:{library}", page_token=first.next_page_token)
        assert [entry.name for entry in first.entries + second.entries] == ["a", "b", "c"]
        assert second.next_page_token is None


@pytest.mark.parametrize("parent", ["site:unknown.example,1,2", "drive:b!unknown", "item:b!unknown:01X"])
async def test_unknown_parents_are_not_found(parent: str) -> None:
    async with graph() as env:
        with pytest.raises(ConnectorNotFoundError):
            await env.connector.browse(ACCESS, parent, page_token=None)


async def test_a_foreign_identifier_makes_no_request() -> None:
    async with graph() as env:
        with pytest.raises(ConnectorNotFoundError):
            await env.connector.browse(ACCESS, "folder-from-google", page_token=None)
        with pytest.raises(ConnectorNotFoundError):
            await env.connector.get_item(ACCESS, "../me/drive")
        assert env.api_requests() == []


# --- items and listing ----------------------------------------------------------------------------------


async def test_a_library_root_is_named_after_the_library() -> None:
    async with graph() as env:
        library = env.microsoft.drive("Contracts Library", site=env.microsoft.site("Legal"))
        root = await env.connector.get_item(ACCESS, f"drive:{library}")
        assert root.id == item_ref(library, env.microsoft.root(library))
        assert (root.name, root.kind, root.parents, root.drive_id) == (
            "Contracts Library",
            RemoteKind.FOLDER,
            (),
            library,
        )
        again = await env.connector.get_item(ACCESS, root.id)  # the item form of the same folder
        assert again.kind is RemoteKind.FOLDER


async def test_get_item_of_a_folder_and_a_site() -> None:
    async with graph() as env:
        site = env.microsoft.site("Legal")
        library = env.microsoft.drive("Documents", site=site)
        folder = env.microsoft.folder(library, "Contracts")
        item = await env.connector.get_item(ACCESS, item_ref(library, folder))
        assert (item.name, item.kind, item.parents) == (
            "Contracts",
            RemoteKind.FOLDER,
            (item_ref(library, env.microsoft.root(library)),),
        )
        site_item = await env.connector.get_item(ACCESS, f"site:{site}")
        assert (site_item.kind, site_item.name) == (RemoteKind.UNSUPPORTED, "Legal")  # not a folder
        with pytest.raises(ConnectorNotFoundError):
            await env.connector.get_item(ACCESS, "site:unknown.example,1,2")
        env.microsoft.delete(library, folder)
        with pytest.raises(ConnectorNotFoundError):
            await env.connector.get_item(ACCESS, item_ref(library, folder))


async def test_listing_pages_through_children() -> None:
    async with graph() as env:
        library = env.microsoft.drive("Documents", site=env.microsoft.site("Legal"))
        folder = env.microsoft.folder(library, "Contracts")
        for name in ("a.txt", "b.txt", "c.txt"):
            env.microsoft.add(library, name, parent=folder, content=name.encode())
        env.microsoft.page_size = 2
        items, token = [], None
        while True:
            page = await env.connector.list_children(ACCESS, item_ref(library, folder), page_token=token)
            items.extend(page.items)
            token = page.next_page_token
            if token is None:
                break
        assert [item.name for item in items] == ["a.txt", "b.txt", "c.txt"]
        assert all(item.parents == (item_ref(library, folder),) for item in items)
        assert all(item.revision is not None and item.revision.startswith("qx:") for item in items)


@pytest.mark.parametrize(
    "link",
    [
        "https://graph.microsoft.com/v1.0/drives/x/children?$skiptoken=2",  # another host than api_url
        "http://127.0.0.1:1/v1.0/drives/x/children?$skiptoken=2",  # another port
        "{base}/beta/drives/x/children?$skiptoken=2",  # outside the API path
        "{base}/v1.0/drives/x/children?page=2",  # no paging parameter
        "{base}/v1.0/drives/x/children?$skiptoken=a%20b",  # not a printable token
    ],
)
async def test_links_outside_the_api_are_refused(link: str) -> None:
    async with graph() as env:
        library = env.microsoft.drive("Documents", site=env.microsoft.site("Legal"))
        base = f"http://127.0.0.1:{env.server.port}"
        env.microsoft.api_failures.append(
            Reply.json({"value": [], "@odata.nextLink": link.format(base=base)})
        )
        with pytest.raises(ConnectorInvalidResponseError):
            await env.connector.list_children(ACCESS, f"drive:{library}", page_token=None)


@pytest.mark.parametrize("token", ["page=2", "=2", "$skiptoken=", "token=" + "x" * 3000, "no-separator"])
async def test_forged_page_tokens_are_refused_without_a_request(token: str) -> None:
    async with graph() as env:
        library = env.microsoft.drive("Documents", site=env.microsoft.site("Legal"))
        with pytest.raises(ConnectorCursorInvalidError):
            await env.connector.list_children(ACCESS, f"drive:{library}", page_token=token)
        assert env.api_requests() == []


# --- delta ----------------------------------------------------------------------------------------------


async def test_delta_starts_now_pages_and_reports_deletions() -> None:
    async with graph() as env:
        library = env.microsoft.drive("Documents", site=env.microsoft.site("Legal"))
        folder = env.microsoft.folder(library, "Contracts")
        old = env.microsoft.add(library, "old.txt", parent=folder, content=b"old")
        location = SourceLocation(folder_id=item_ref(library, folder), drive_id=library)
        cursor = await env.connector.start_cursor(ACCESS, location)
        assert cursor == "token=2"  # "latest": nothing that happened before is replayed

        new = env.microsoft.add(library, "new.txt", parent=folder, content=b"new")
        env.microsoft.change(library, old, name="renamed.txt")
        env.microsoft.delete(library, new)
        env.microsoft.page_size = 2
        first = await env.connector.get_changes(ACCESS, location, cursor)
        assert not first.finished
        assert first.cursor == "token=4"
        second = await env.connector.get_changes(ACCESS, location, first.cursor)
        assert second.finished
        assert second.cursor == "token=5"
        changes = first.changes + second.changes
        assert [(change.item_id, change.removed) for change in changes] == [
            (item_ref(library, new), True),  # the log shows its latest state: deleted
            (item_ref(library, old), False),
            (item_ref(library, new), True),
        ]
        assert changes[1].item is not None
        assert changes[1].item.name == "renamed.txt"
        idle = await env.connector.get_changes(ACCESS, location, second.cursor)
        assert (idle.changes, idle.finished, idle.cursor) == ((), True, "token=5")


async def test_an_expired_delta_token_is_an_invalid_cursor() -> None:
    async with graph() as env:
        library = env.microsoft.drive("Documents", site=env.microsoft.site("Legal"))
        location = SourceLocation(folder_id=f"drive:{library}", drive_id=library)
        with pytest.raises(ConnectorCursorInvalidError):  # 410 resyncRequired
            await env.connector.get_changes(ACCESS, location, "token=999")
        env.microsoft.api_failures.append(Reply.json({"error": {"code": "invalidRequest"}}, status=400))
        with pytest.raises(ConnectorCursorInvalidError):
            await env.connector.get_changes(ACCESS, location, "token=0")
        with pytest.raises(ConnectorCursorInvalidError):  # not a cursor this adapter gave out
            await env.connector.get_changes(ACCESS, location, "https://graph.microsoft.com/v1.0/delta")


async def test_the_drive_comes_from_the_location_or_the_folder() -> None:
    async with graph() as env:
        library = env.microsoft.drive("Documents", site=env.microsoft.site("Legal"))
        without_drive = SourceLocation(folder_id=item_ref(library, env.microsoft.root(library)))
        assert await env.connector.start_cursor(ACCESS, without_drive) == "token=0"
        with pytest.raises(ConnectorNotFoundError):
            await env.connector.start_cursor(ACCESS, SourceLocation(folder_id="folder-from-google"))


async def test_delta_needs_a_delta_link_at_the_end() -> None:
    async with graph() as env:
        library = env.microsoft.drive("Documents", site=env.microsoft.site("Legal"))
        env.microsoft.api_failures.append(Reply.json({"value": []}))
        with pytest.raises(ConnectorInvalidResponseError):
            await env.connector.start_cursor(ACCESS, SourceLocation(f"drive:{library}", library))


# --- downloads and errors -------------------------------------------------------------------------------


async def test_a_file_is_downloaded_as_is() -> None:
    async with graph() as env:
        library = env.microsoft.drive("Documents", site=env.microsoft.site("Legal"))
        file_id = env.microsoft.add(library, "a.docx", content=b"PK\x03\x04 content")
        item = await env.connector.get_item(ACCESS, item_ref(library, file_id))
        assert await _read(env.connector.download(ACCESS, item, max_bytes=1000)) == b"PK\x03\x04 content"
        assert env.api_requests()[-1].target.endswith("/content")


async def test_downloads_are_bounded_and_only_files() -> None:
    async with graph() as env:
        library = env.microsoft.drive("Documents", site=env.microsoft.site("Legal"))
        file_id = env.microsoft.add(library, "big.bin", content=b"x" * 100)
        folder = env.microsoft.folder(library, "Contracts")
        item = await env.connector.get_item(ACCESS, item_ref(library, file_id))
        before = len(env.api_requests())
        with pytest.raises(ConnectorFileTooLargeError):
            await _read(env.connector.download(ACCESS, item, max_bytes=10))
        assert len(env.api_requests()) == before  # refused before downloading
        with pytest.raises(ValueError, match="only files"):
            await _read(
                env.connector.download(
                    ACCESS, await env.connector.get_item(ACCESS, item_ref(library, folder)), max_bytes=10
                )
            )


async def test_a_download_redirect_outside_the_connector_hosts_is_refused() -> None:
    async with graph() as env:
        library = env.microsoft.drive("Documents", site=env.microsoft.site("Legal"))
        file_id = env.microsoft.add(library, "a.txt", content=b"abc")
        item = await env.connector.get_item(ACCESS, item_ref(library, file_id))
        env.microsoft.download_redirect = "https://evil.sharepoint.com.example"
        with pytest.raises(ConnectorEgressDeniedError):
            await _read(env.connector.download(ACCESS, item, max_bytes=100))


async def test_throttling_is_retried_with_retry_after() -> None:
    async with graph() as env:
        library = env.microsoft.drive("Documents", site=env.microsoft.site("Legal"))
        env.microsoft.api_failures.extend(
            [
                Reply.json({"error": {"code": "activityLimitReached"}}, status=429, **{"Retry-After": "7"}),
                Reply.json({"error": {"code": "serviceNotAvailable"}}, status=503, **{"Retry-After": "3"}),
            ]
        )
        page = await env.connector.list_children(ACCESS, f"drive:{library}", page_token=None)
        assert page.items == ()
        assert env.sleeps == [7.0, 3.0]


async def test_throttling_gives_up_after_the_attempts() -> None:
    async with graph(max_attempts=2) as env:
        library = env.microsoft.drive("Documents", site=env.microsoft.site("Legal"))
        env.microsoft.api_failures.extend([Reply.json({}, status=429), Reply.json({}, status=429)])
        with pytest.raises(ConnectorRateLimitedError):
            await env.connector.list_children(ACCESS, f"drive:{library}", page_token=None)


async def test_a_rejected_token_and_denied_access_are_not_retried() -> None:
    async with graph() as env:
        library = env.microsoft.drive("Documents", site=env.microsoft.site("Legal"))
        env.microsoft.api_failures.append(Reply.json({"error": {"code": "accessDenied"}}, status=403))
        with pytest.raises(ConnectorPermissionDeniedError):
            await env.connector.list_children(ACCESS, f"drive:{library}", page_token=None)
        env.microsoft.access_tokens.clear()
        with pytest.raises(ConnectorAccessTokenRejectedError):
            await env.connector.list_children(ACCESS, f"drive:{library}", page_token=None)
        assert env.sleeps == []


async def test_the_token_never_appears_in_a_url() -> None:
    async with graph() as env:
        library = env.microsoft.drive("Documents", site=env.microsoft.site("Legal"))
        env.microsoft.add(library, "a.txt", content=b"abc")
        await env.connector.browse(ACCESS, None, page_token=None)
        await env.connector.list_children(ACCESS, f"drive:{library}", page_token=None)
        await env.connector.start_cursor(ACCESS, SourceLocation(f"drive:{library}", library))
        assert env.api_requests()
        assert all(TOKEN not in request.target for request in env.server.requests)
        assert all(request.headers["authorization"] == f"Bearer {TOKEN}" for request in env.api_requests())


def test_capabilities_say_there_is_no_revoke() -> None:
    connector = MicrosoftGraphConnector(
        SafeHttpClient(build_egress_policy(parse_instance_config(b""))), MicrosoftEndpoints.for_tenant(TENANT)
    )
    capabilities = connector.capabilities()
    assert (capabilities.revoke, capabilities.incremental_changes, capabilities.folder_picker) == (
        False,
        True,
        True,
    )
    assert capabilities.exports == {}
