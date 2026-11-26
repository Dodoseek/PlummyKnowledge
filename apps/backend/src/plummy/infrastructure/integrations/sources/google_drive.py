"""Google Drive v3 as a :class:`DataSourceConnector` (ADR-020 §4, §5, §8). Plain HTTP through the shared
:class:`ConnectorTransport` (retries, bounded downloads); no Google SDK.

Every call carries the access token the caller obtained; it is never stored or logged. ``429`` and the
rate-limit reasons of ``403`` are retried like ``5xx`` (``google_common.api_error`` makes them retryable).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable, Mapping
from datetime import datetime
from typing import Any, Final
from urllib.parse import quote

import httpx

from plummy.application.sources.ports import (
    BrowseEntry,
    BrowseKind,
    BrowsePage,
    ChangesPage,
    ConnectorCapabilities,
    ConnectorCursorInvalidError,
    ConnectorError,
    ConnectorFileTooLargeError,
    ConnectorInvalidResponseError,
    RemoteChange,
    RemoteItem,
    RemoteKind,
    RemotePage,
    SourceLocation,
)
from plummy.domain.knowledge import DataSourceType
from plummy.domain.secrets import SecretValue
from plummy.infrastructure.http.safe_client import SafeHttpClient
from plummy.infrastructure.integrations.sources.connector_http import ConnectorTransport, Sleep
from plummy.infrastructure.integrations.sources.google_common import PROVIDER_TYPE, GoogleEndpoints, api_error

logger = logging.getLogger(__name__)

FOLDER_MIME: Final = "application/vnd.google-apps.folder"
SHORTCUT_MIME: Final = "application/vnd.google-apps.shortcut"
GOOGLE_APPS_PREFIX: Final = "application/vnd.google-apps."
DOCX: Final = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX: Final = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PDF: Final = "application/pdf"
EXPORTS: Final[Mapping[str, tuple[str, str]]] = {
    "application/vnd.google-apps.document": (DOCX, ".docx"),
    "application/vnd.google-apps.spreadsheet": (XLSX, ".xlsx"),
    "application/vnd.google-apps.presentation": (PDF, ".pdf"),
}
FILE_FIELDS: Final = "id,name,mimeType,parents,trashed,size,md5Checksum,version,modifiedTime,driveId"
CHANGES_FIELDS: Final = (
    f"nextPageToken,newStartPageToken,changes(changeType,removed,fileId,file({FILE_FIELDS}))"
)
BROWSE_PAGE_SIZE: Final = 100
LIST_PAGE_SIZE: Final = 1000
MAX_SHARED_DRIVES: Final = 100  # the top level of the picker shows the first page of shared drives
MAX_ID_LENGTH: Final = 256
MAX_NAME_LENGTH: Final = 1024
MAX_TOKEN_LENGTH: Final = 2048


def _quote(value: str) -> str:
    """A string literal of the Drive query language."""
    return "'" + value.replace("\\", "\\\\").replace("'", "\\'") + "'"


def _string(data: Mapping[str, Any], key: str, *, limit: int, required: bool = True) -> str | None:
    value = data.get(key)
    if value is None and not required:
        return None
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ConnectorInvalidResponseError(f"invalid {key}")
    return value


def _required(data: Mapping[str, Any], key: str, *, limit: int = MAX_ID_LENGTH) -> str:
    value = _string(data, key, limit=limit)
    if value is None:  # pragma: no cover - _string raises for a missing required value
        raise ConnectorInvalidResponseError(f"invalid {key}")
    return value


def _token(data: Mapping[str, Any], key: str) -> str | None:
    return _string(data, key, limit=MAX_TOKEN_LENGTH, required=False)


def _name(data: Mapping[str, Any]) -> str:
    value = data.get("name")
    if not isinstance(value, str):
        raise ConnectorInvalidResponseError("invalid name")
    return value[:MAX_NAME_LENGTH] or "(untitled)"


def _size(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, str) and value.isdigit():
        return int(value)
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    raise ConnectorInvalidResponseError("invalid size")


def _time(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConnectorInvalidResponseError("invalid modifiedTime")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise ConnectorInvalidResponseError("invalid modifiedTime") from None
    if moment.tzinfo is None:
        raise ConnectorInvalidResponseError("invalid modifiedTime")
    return moment


def parse_item(data: Any) -> RemoteItem:
    """A ``files`` resource, checked field by field."""
    if not isinstance(data, dict):
        raise ConnectorInvalidResponseError("invalid file")
    item_id = _required(data, "id")
    mime = _required(data, "mimeType", limit=255)
    name = _name(data)
    parents_raw = data.get("parents", [])
    if not isinstance(parents_raw, list) or not all(
        isinstance(parent, str) and 0 < len(parent) <= MAX_ID_LENGTH for parent in parents_raw
    ):
        raise ConnectorInvalidResponseError("invalid parents")
    trashed = data.get("trashed", False)
    if not isinstance(trashed, bool):
        raise ConnectorInvalidResponseError("invalid trashed")
    checksum = _string(data, "md5Checksum", limit=64, required=False)
    version = data.get("version")
    if version is not None and not isinstance(version, (str, int)):
        raise ConnectorInvalidResponseError("invalid version")
    filename, content_type = name, mime
    if mime == FOLDER_MIME:
        kind = RemoteKind.FOLDER
    elif mime == SHORTCUT_MIME:
        kind = RemoteKind.SHORTCUT
    elif mime in EXPORTS:
        kind = RemoteKind.FILE
        content_type, extension = EXPORTS[mime]
        filename = name if name.lower().endswith(extension) else name + extension
    elif mime.startswith(GOOGLE_APPS_PREFIX):
        kind = RemoteKind.UNSUPPORTED
    else:
        kind = RemoteKind.FILE
    if checksum is not None and not mime.startswith(GOOGLE_APPS_PREFIX):
        revision: str | None = "md5:" + checksum
    else:
        revision = f"v:{version}" if version is not None else None
    return RemoteItem(
        id=item_id,
        name=name,
        kind=kind,
        mime_type=mime,
        parents=tuple(parents_raw),
        trashed=trashed,
        size=_size(data.get("size")),
        revision=revision,
        modified_at=_time(data.get("modifiedTime")),
        drive_id=_string(data, "driveId", limit=MAX_ID_LENGTH, required=False),
        filename=filename,
        content_type=content_type,
    )


def _list(data: Mapping[str, Any], key: str) -> list[Any]:
    value = data.get(key, [])
    if not isinstance(value, list):
        raise ConnectorInvalidResponseError(f"invalid {key}")
    return value


class GoogleDriveConnector:
    def __init__(
        self,
        http: SafeHttpClient,
        endpoints: GoogleEndpoints | None = None,
        *,
        max_attempts: int = 5,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._endpoints = endpoints or GoogleEndpoints()
        self._transport = ConnectorTransport(
            http, provider_type=PROVIDER_TYPE, api_error=api_error, max_attempts=max_attempts, sleep=sleep
        )

    @property
    def source_type(self) -> DataSourceType:
        return DataSourceType.GOOGLE_DRIVE

    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(
            incremental_changes=True,
            folder_picker=True,
            revoke=True,
            exports={source: target for source, (target, _) in EXPORTS.items()},
        )

    # --- transport ----------------------------------------------------------------------------------------

    def _url(self, path: str) -> str:
        return f"{self._endpoints.api_url}/drive/v3/{path}"

    async def _get(
        self,
        access_token: SecretValue,
        path: str,
        params: Mapping[str, str],
        on_error: Callable[[httpx.Response], ConnectorError] | None = None,
    ) -> dict[str, Any]:
        return await self._transport.get_json(access_token, self._url(path), params, on_error)

    # --- reading --------------------------------------------------------------------------------------------

    async def browse(
        self, access_token: SecretValue, parent_id: str | None, *, page_token: str | None
    ) -> BrowsePage:
        params = {
            "pageSize": str(BROWSE_PAGE_SIZE),
            "orderBy": "name",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
            "fields": "nextPageToken,files(id,name)",
        }
        if parent_id is None:
            params["q"] = (
                f"mimeType = {_quote(FOLDER_MIME)} and trashed = false "
                "and ('root' in parents or sharedWithMe = true)"
            )
            params["corpora"] = "user"
        else:
            params["q"] = (
                f"{_quote(parent_id)} in parents and mimeType = {_quote(FOLDER_MIME)} and trashed = false"
            )
            params["corpora"] = "allDrives"
        if page_token is not None:
            params["pageToken"] = page_token
        entries: list[BrowseEntry] = []
        if parent_id is None and page_token is None:
            drives = await self._get(
                access_token,
                "drives",
                {"pageSize": str(MAX_SHARED_DRIVES), "fields": "drives(id,name)"},
            )
            for drive in _list(drives, "drives"):
                if not isinstance(drive, dict):
                    raise ConnectorInvalidResponseError("invalid drive")
                drive_id = _required(drive, "id")
                entries.append(BrowseEntry(drive_id, _name(drive), BrowseKind.SHARED_DRIVE))
        data = await self._get(access_token, "files", params)
        for raw in _list(data, "files"):
            if not isinstance(raw, dict):
                raise ConnectorInvalidResponseError("invalid file")
            folder_id = _required(raw, "id")
            entries.append(BrowseEntry(folder_id, _name(raw), BrowseKind.FOLDER))
        return BrowsePage(tuple(entries), _token(data, "nextPageToken"))

    async def get_item(self, access_token: SecretValue, item_id: str) -> RemoteItem:
        data = await self._get(
            access_token,
            "files/" + quote(item_id, safe=""),
            {"fields": FILE_FIELDS, "supportsAllDrives": "true"},
        )
        return parse_item(data)

    async def list_children(
        self, access_token: SecretValue, folder_id: str, *, page_token: str | None
    ) -> RemotePage:
        params = {
            "q": f"{_quote(folder_id)} in parents and trashed = false",
            "pageSize": str(LIST_PAGE_SIZE),
            "corpora": "allDrives",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
            "fields": f"nextPageToken,files({FILE_FIELDS})",
        }
        if page_token is not None:
            params["pageToken"] = page_token
        data = await self._get(access_token, "files", params)
        items = tuple(parse_item(raw) for raw in _list(data, "files"))
        return RemotePage(items, _token(data, "nextPageToken"))

    async def start_cursor(self, access_token: SecretValue, location: SourceLocation) -> str:
        del location  # the Changes API covers the whole account (ADR-020 §8)
        data = await self._get(access_token, "changes/startPageToken", {"supportsAllDrives": "true"})
        cursor = _token(data, "startPageToken")
        if cursor is None:
            raise ConnectorInvalidResponseError("no startPageToken")
        return cursor

    async def get_changes(
        self, access_token: SecretValue, location: SourceLocation, cursor: str
    ) -> ChangesPage:
        del location

        def on_error(response: httpx.Response) -> ConnectorError:
            if response.status_code in (400, 404):
                return ConnectorCursorInvalidError()  # the request is fixed but for the token
            return api_error(response)

        data = await self._get(
            access_token,
            "changes",
            {
                "pageToken": cursor,
                "pageSize": str(LIST_PAGE_SIZE),
                "spaces": "drive",
                "includeRemoved": "true",
                "includeItemsFromAllDrives": "true",
                "supportsAllDrives": "true",
                "fields": CHANGES_FIELDS,
            },
            on_error,
        )
        changes: list[RemoteChange] = []
        for raw in _list(data, "changes"):
            if not isinstance(raw, dict):
                raise ConnectorInvalidResponseError("invalid change")
            if raw.get("changeType", "file") != "file":
                continue  # shared drive settings; their files arrive as file changes
            file_id = _required(raw, "fileId")
            removed = raw.get("removed", False) is True
            file = raw.get("file")
            item = None if removed or file is None else parse_item(file)
            changes.append(RemoteChange(file_id, removed or item is None, item))
        following, start = _token(data, "nextPageToken"), _token(data, "newStartPageToken")
        if following is not None:
            return ChangesPage(tuple(changes), following, finished=False)
        if start is None:
            raise ConnectorInvalidResponseError("no page token")
        return ChangesPage(tuple(changes), start, finished=True)

    # --- downloading ----------------------------------------------------------------------------------------

    def _download_request(self, item: RemoteItem) -> tuple[str, dict[str, str]]:
        path = "files/" + quote(item.id, safe="")
        if item.mime_type in EXPORTS:
            return self._url(path + "/export"), {"mimeType": item.content_type}
        return self._url(path), {"alt": "media", "supportsAllDrives": "true"}

    async def download(
        self, access_token: SecretValue, item: RemoteItem, *, max_bytes: int
    ) -> AsyncIterator[bytes]:
        if item.kind is not RemoteKind.FILE:
            raise ValueError("only files can be downloaded")
        if item.size is not None and item.size > max_bytes:
            raise ConnectorFileTooLargeError()
        url, params = self._download_request(item)
        async for chunk in self._transport.download(
            access_token, url, params, max_bytes=max_bytes, redirects=self._endpoints.redirects
        ):
            yield chunk
