"""Microsoft Graph v1.0 (OneDrive and SharePoint document libraries) as a :class:`DataSourceConnector`
(ADR-021 §4-§7). Plain HTTP through the shared :class:`ConnectorTransport`; no Graph SDK.

Identifiers given out are opaque to the client and carry the drive, as Graph needs it for every item:
``site:{site_id}`` (a site: opened in the picker, never chosen), ``drive:{drive_id}`` (a library or a
OneDrive; choosing it means its root folder) and ``item:{drive_id}:{item_id}`` (a folder or a file; the ``id``
and ``parents`` of every :class:`RemoteItem`, so documents, folder paths and the chosen folder use one form).

``@odata.nextLink`` and ``@odata.deltaLink`` are never requested as they are: only their paging parameter is
kept, and the next request is built again from the fixed ``api_url`` (ADR-021 §5). A cursor is that parameter
as ``name=value`` (``token``, ``$skiptoken`` or ``$deltatoken``), at most 2048 characters.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import AsyncIterator, Mapping
from datetime import datetime
from typing import Any, Final
from urllib.parse import parse_qs, quote, urlsplit

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
    ConnectorNotFoundError,
    RemoteChange,
    RemoteItem,
    RemoteKind,
    RemotePage,
    SourceLocation,
)
from plummy.domain.knowledge import DataSourceType
from plummy.domain.secrets import SecretValue
from plummy.infrastructure.http.safe_client import SafeHttpClient
from plummy.infrastructure.integrations.sources.connector_http import ConnectorTransport, ErrorMapper, Sleep
from plummy.infrastructure.integrations.sources.microsoft_common import (
    PROVIDER_TYPE,
    MicrosoftEndpoints,
    api_error,
)

ITEM_FIELDS: Final = (
    "id,name,folder,file,package,remoteItem,deleted,root,parentReference,size,lastModifiedDateTime,cTag"
)
FOLDER_MIME: Final = "application/vnd.microsoft.folder"
OCTET_STREAM: Final = "application/octet-stream"
LIST_PAGE_SIZE: Final = 200
MAX_SITES: Final = 100  # the top level of the picker shows the first sites the search returns
MAX_ID_LENGTH: Final = 256
MAX_NAME_LENGTH: Final = 1024
MAX_CURSOR_LENGTH: Final = 2048
# drive "b!…", item "01ABC…" or "ABC!123", site "host,guid,guid"
_RAW_ID: Final = re.compile(r"[A-Za-z0-9!_.,-]{1,256}")
_PAGING_PARAMETERS: Final = ("token", "$skiptoken", "$deltatoken")
_CURSOR: Final = re.compile(r"[!-~]{1,2048}")


# --- identifiers ------------------------------------------------------------------------------------------


def _raw_id(value: Any, what: str) -> str:
    if not isinstance(value, str) or _RAW_ID.fullmatch(value) is None:
        raise ConnectorInvalidResponseError(f"invalid {what}")
    return value


def item_ref(drive_id: str, item_id: str) -> str:
    return f"item:{drive_id}:{item_id}"


def drive_ref(drive_id: str) -> str:
    return f"drive:{drive_id}"


def site_ref(site_id: str) -> str:
    return f"site:{site_id}"


def parse_ref(value: str) -> tuple[str, str, str | None]:
    """``(kind, id, item_id)`` of an identifier this adapter gave out; anything else is not found."""
    kind, _, rest = value.partition(":")
    if kind in ("site", "drive") and _RAW_ID.fullmatch(rest):
        return kind, rest, None
    if kind == "item":
        drive_id, _, item_id = rest.partition(":")
        if _RAW_ID.fullmatch(drive_id) and _RAW_ID.fullmatch(item_id):
            return kind, drive_id, item_id
    raise ConnectorNotFoundError()


# --- parsing ----------------------------------------------------------------------------------------------


def _name(data: Mapping[str, Any]) -> str:
    value = data.get("name")
    if not isinstance(value, str):
        raise ConnectorInvalidResponseError("invalid name")
    return value[:MAX_NAME_LENGTH] or "(untitled)"


def _size(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    raise ConnectorInvalidResponseError("invalid size")


def _time(value: Any) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConnectorInvalidResponseError("invalid lastModifiedDateTime")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        raise ConnectorInvalidResponseError("invalid lastModifiedDateTime") from None
    if moment.tzinfo is None:
        raise ConnectorInvalidResponseError("invalid lastModifiedDateTime")
    return moment


def _facet(data: Mapping[str, Any], key: str) -> dict[str, Any] | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ConnectorInvalidResponseError(f"invalid {key}")
    return value


def _hash(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not 0 < len(value) <= 128:
        raise ConnectorInvalidResponseError("invalid hash")
    return value


def _revision(file: Mapping[str, Any] | None, data: Mapping[str, Any]) -> str | None:
    """Changes exactly when the content does (ADR-021 §7): a hash, else ``cTag``; never ``eTag``."""
    hashes = _facet(file, "hashes") if file is not None else None
    if hashes is not None:
        for key, prefix in (("quickXorHash", "qx"), ("sha256Hash", "sha256"), ("sha1Hash", "sha1")):
            value = _hash(hashes.get(key))
            if value is not None:
                return f"{prefix}:{value}"
    ctag = data.get("cTag")
    if ctag is None:
        return None
    if not isinstance(ctag, str) or not 0 < len(ctag) <= 512:
        raise ConnectorInvalidResponseError("invalid cTag")
    return f"ctag:{ctag}"


def parse_item(data: Any, drive_id: str) -> RemoteItem:
    """A ``driveItem`` of ``drive_id``, checked field by field."""
    if not isinstance(data, dict):
        raise ConnectorInvalidResponseError("invalid item")
    item_id = _raw_id(data.get("id"), "id")
    parent = _facet(data, "parentReference") or {}
    other_drive = parent.get("driveId")
    if other_drive is not None and (
        not isinstance(other_drive, str) or other_drive.lower() != drive_id.lower()
    ):
        raise ConnectorInvalidResponseError("item of another drive")
    parent_id = parent.get("id")
    parents = (item_ref(drive_id, _raw_id(parent_id, "parentReference")),) if parent_id is not None else ()
    deleted = _facet(data, "deleted") is not None
    file = _facet(data, "file")
    name = "(deleted)" if deleted and "name" not in data else _name(data)
    mime = OCTET_STREAM
    if _facet(data, "folder") is not None or _facet(data, "root") is not None:
        kind, mime = RemoteKind.FOLDER, FOLDER_MIME
    elif _facet(data, "remoteItem") is not None:
        kind = RemoteKind.SHORTCUT  # an item of another drive: never followed (ADR-020 §8)
    elif _facet(data, "package") is not None:
        kind = RemoteKind.UNSUPPORTED  # OneNote notebooks and other packages (ADR-021 §7)
    elif file is not None:
        kind = RemoteKind.FILE
        declared = file.get("mimeType")
        if isinstance(declared, str) and 0 < len(declared) <= 255:
            mime = declared
    else:
        kind = RemoteKind.UNSUPPORTED
    return RemoteItem(
        id=item_ref(drive_id, item_id),
        name=name,
        kind=kind,
        mime_type=mime,
        parents=parents,
        trashed=deleted,
        size=_size(data.get("size")) if kind is RemoteKind.FILE else None,
        revision=_revision(file, data) if kind is RemoteKind.FILE else None,
        modified_at=_time(data.get("lastModifiedDateTime")),
        drive_id=drive_id,
        filename=name,
        content_type=mime,
    )


def _list(data: Mapping[str, Any], key: str = "value") -> list[Any]:
    value = data.get(key, [])
    if not isinstance(value, list):
        raise ConnectorInvalidResponseError(f"invalid {key}")
    return value


# --- the connector ----------------------------------------------------------------------------------------


class MicrosoftGraphConnector:
    def __init__(
        self,
        http: SafeHttpClient,
        endpoints: MicrosoftEndpoints,
        *,
        max_attempts: int = 5,
        sleep: Sleep = asyncio.sleep,
    ) -> None:
        self._endpoints = endpoints
        api = urlsplit(endpoints.api_url)
        self._api_origin = (api.scheme.lower(), api.netloc.lower())
        self._api_path = api.path.rstrip("/") + "/"
        self._transport = ConnectorTransport(
            http, provider_type=PROVIDER_TYPE, api_error=api_error, max_attempts=max_attempts, sleep=sleep
        )

    @property
    def source_type(self) -> DataSourceType:
        return DataSourceType.MICROSOFT_365

    def capabilities(self) -> ConnectorCapabilities:
        return ConnectorCapabilities(incremental_changes=True, folder_picker=True, revoke=False, exports={})

    # --- transport --------------------------------------------------------------------------------------

    def _url(self, *segments: str) -> str:
        return self._endpoints.api_url + "/" + "/".join(quote(segment, safe="") for segment in segments)

    async def _get(
        self,
        access_token: SecretValue,
        segments: tuple[str, ...],
        params: Mapping[str, str],
        on_error: ErrorMapper | None = None,
    ) -> dict[str, Any]:
        return await self._transport.get_json(access_token, self._url(*segments), params, on_error)

    def _paging(self, data: Mapping[str, Any], key: str) -> str | None:
        """``name=value`` of the paging parameter of ``@odata.nextLink`` / ``@odata.deltaLink``."""
        link = data.get(key)
        if link is None:
            return None
        if not isinstance(link, str) or len(link) > 8192:
            raise ConnectorInvalidResponseError(f"invalid {key}")
        parts = urlsplit(link)
        if (parts.scheme.lower(), parts.netloc.lower()) != self._api_origin or not (
            parts.path + "/"
        ).startswith(self._api_path):
            raise ConnectorInvalidResponseError(f"{key} outside the API")
        query = parse_qs(parts.query)
        for name in _PAGING_PARAMETERS:
            values = query.get(name)
            if values:
                cursor = f"{name}={values[0]}"
                if _CURSOR.fullmatch(cursor) is None:
                    raise ConnectorInvalidResponseError(f"invalid {key}")
                return cursor
        raise ConnectorInvalidResponseError(f"{key} without a paging parameter")

    @staticmethod
    def _cursor_params(cursor: str | None) -> dict[str, str]:
        if cursor is None:
            return {}
        name, separator, value = cursor.partition("=")
        if not separator or name not in _PAGING_PARAMETERS or not value or len(cursor) > MAX_CURSOR_LENGTH:
            raise ConnectorCursorInvalidError()
        return {name: value}

    # --- reading ----------------------------------------------------------------------------------------

    async def browse(
        self, access_token: SecretValue, parent_id: str | None, *, page_token: str | None
    ) -> BrowsePage:
        if parent_id is None:
            return await self._browse_top(access_token)
        kind, ident, item_id = parse_ref(parent_id)
        if kind == "site":
            data = await self._get(access_token, ("sites", ident, "drives"), {"$select": "id,name,driveType"})
            drives: list[BrowseEntry] = []
            for raw in _list(data):
                if not isinstance(raw, dict):
                    raise ConnectorInvalidResponseError("invalid drive")
                drives.append(
                    BrowseEntry(drive_ref(_raw_id(raw.get("id"), "drive")), _name(raw), BrowseKind.DRIVE)
                )
            return BrowsePage(tuple(drives), None)
        page = await self._children(access_token, ident, item_id, page_token)
        entries = tuple(
            BrowseEntry(item.id, item.name, BrowseKind.FOLDER)
            for item in page.items
            if item.kind is RemoteKind.FOLDER and not item.trashed
        )
        return BrowsePage(entries, page.next_page_token)

    async def _browse_top(self, access_token: SecretValue) -> BrowsePage:
        """The account's OneDrive and the first sites it can see (ADR-021 §6)."""
        entries: list[BrowseEntry] = []
        try:
            own = await self._get(access_token, ("me", "drive"), {"$select": "id,name,driveType"})
        except ConnectorNotFoundError:
            own = None  # no OneDrive licence: only sites
        if own is not None:
            entries.append(
                BrowseEntry(drive_ref(_raw_id(own.get("id"), "drive")), _name(own), BrowseKind.DRIVE)
            )
        sites = await self._get(access_token, ("sites",), {"search": "*", "$select": "id,displayName,name"})
        for raw in _list(sites)[:MAX_SITES]:
            if not isinstance(raw, dict):
                raise ConnectorInvalidResponseError("invalid site")
            label = raw.get("displayName") if isinstance(raw.get("displayName"), str) else raw.get("name")
            name = label[:MAX_NAME_LENGTH] if isinstance(label, str) and label else "(untitled)"
            entries.append(BrowseEntry(site_ref(_raw_id(raw.get("id"), "site")), name, BrowseKind.SITE))
        return BrowsePage(tuple(entries), None)

    async def _children(
        self, access_token: SecretValue, drive_id: str, item_id: str | None, page_token: str | None
    ) -> RemotePage:
        segments = (
            ("drives", drive_id, "root", "children")
            if item_id is None
            else ("drives", drive_id, "items", item_id, "children")
        )
        params = {"$top": str(LIST_PAGE_SIZE), "$select": ITEM_FIELDS} | self._cursor_params(page_token)
        data = await self._get(access_token, segments, params)
        items = tuple(parse_item(raw, drive_id) for raw in _list(data))
        return RemotePage(items, self._paging(data, "@odata.nextLink"))

    async def get_item(self, access_token: SecretValue, item_id: str) -> RemoteItem:
        kind, ident, inner = parse_ref(item_id)
        if kind == "site":
            return await self._site_item(access_token, ident)
        if inner is not None:
            return parse_item(
                await self._get(access_token, ("drives", ident, "items", inner), {"$select": ITEM_FIELDS}),
                ident,
            )
        root = parse_item(
            await self._get(access_token, ("drives", ident, "root"), {"$select": ITEM_FIELDS}), ident
        )
        drive = await self._get(access_token, ("drives", ident), {"$select": "id,name"})
        return RemoteItem(  # the library's root is named after the library, not "root"
            id=root.id,
            name=_name(drive),
            kind=root.kind,
            mime_type=root.mime_type,
            parents=(),
            trashed=root.trashed,
            size=None,
            revision=None,
            modified_at=root.modified_at,
            drive_id=ident,
            filename=_name(drive),
            content_type=root.content_type,
        )

    async def _site_item(self, access_token: SecretValue, site_id: str) -> RemoteItem:
        """A site exists but is not a folder: it is opened in the picker, never chosen (ADR-021 §6)."""
        data = await self._get(access_token, ("sites", site_id), {"$select": "id,displayName,name"})
        label = data.get("displayName") if isinstance(data.get("displayName"), str) else data.get("name")
        name = label[:MAX_NAME_LENGTH] if isinstance(label, str) and label else "(untitled)"
        return RemoteItem(
            id=site_ref(site_id),
            name=name,
            kind=RemoteKind.UNSUPPORTED,
            mime_type="application/vnd.microsoft.site",
            parents=(),
            trashed=False,
            size=None,
            revision=None,
            modified_at=None,
            drive_id=None,
            filename=name,
            content_type="application/vnd.microsoft.site",
        )

    async def list_children(
        self, access_token: SecretValue, folder_id: str, *, page_token: str | None
    ) -> RemotePage:
        kind, ident, inner = parse_ref(folder_id)
        if kind == "site":
            raise ConnectorNotFoundError()
        return await self._children(access_token, ident, inner, page_token)

    @staticmethod
    def _drive_of(location: SourceLocation) -> str:
        drive_id = location.drive_id
        if drive_id is None:
            _, drive_id, _ = parse_ref(location.folder_id)
        if _RAW_ID.fullmatch(drive_id) is None:
            raise ConnectorNotFoundError()
        return drive_id

    @staticmethod
    def _delta_error(response: httpx.Response) -> ConnectorError:
        if response.status_code in (400, 410):  # 410 resyncRequired; the request is fixed but for the token
            return ConnectorCursorInvalidError()
        return api_error(response)

    async def start_cursor(self, access_token: SecretValue, location: SourceLocation) -> str:
        """Delta of "now" without listing the drive (``token=latest``), taken before the full listing."""
        drive_id = self._drive_of(location)
        data = await self._get(
            access_token, ("drives", drive_id, "root", "delta"), {"token": "latest", "$select": "id"}
        )
        cursor = self._paging(data, "@odata.deltaLink")
        if cursor is None:
            raise ConnectorInvalidResponseError("no deltaLink")
        return cursor

    async def get_changes(
        self, access_token: SecretValue, location: SourceLocation, cursor: str
    ) -> ChangesPage:
        drive_id = self._drive_of(location)
        data = await self._get(
            access_token,
            ("drives", drive_id, "root", "delta"),
            {"$select": ITEM_FIELDS} | self._cursor_params(cursor),
            self._delta_error,
        )
        changes: list[RemoteChange] = []
        for raw in _list(data):
            if not isinstance(raw, dict):
                raise ConnectorInvalidResponseError("invalid item")
            if _facet(raw, "deleted") is not None:  # removed items carry no parent: matched by their ID
                changes.append(RemoteChange(item_ref(drive_id, _raw_id(raw.get("id"), "id")), True, None))
                continue
            item = parse_item(raw, drive_id)
            changes.append(RemoteChange(item.id, False, item))
        following = self._paging(data, "@odata.nextLink")
        if following is not None:
            return ChangesPage(tuple(changes), following, finished=False)
        latest = self._paging(data, "@odata.deltaLink")
        if latest is None:
            raise ConnectorInvalidResponseError("no deltaLink")
        return ChangesPage(tuple(changes), latest, finished=True)

    # --- downloading ------------------------------------------------------------------------------------

    async def download(
        self, access_token: SecretValue, item: RemoteItem, *, max_bytes: int
    ) -> AsyncIterator[bytes]:
        if item.kind is not RemoteKind.FILE:
            raise ValueError("only files can be downloaded")
        if item.size is not None and item.size > max_bytes:
            raise ConnectorFileTooLargeError()
        _, drive_id, item_id = parse_ref(item.id)
        if item_id is None:
            raise ValueError("only items can be downloaded")
        # ``…/content`` answers 302 to a pre-authenticated SharePoint URL: followed to the download hosts
        # only, without ``Authorization`` (the URL carries its own short-lived grant and is never logged).
        url = self._url("drives", drive_id, "items", item_id, "content")
        async for chunk in self._transport.download(
            access_token, url, {}, max_bytes=max_bytes, redirects=self._endpoints.redirects
        ):
            yield chunk
