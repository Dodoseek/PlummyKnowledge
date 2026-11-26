"""S3-compatible implementation of ``StorageProvider`` (ADR-022).

Objects live at ``/{bucket}/{prefix}{area}/{last two key characters}/{key}`` (path-style), the same layout as
the local backend. Requests are signed with SigV4 and sent through ``SafeHttpClient`` (purpose ``storage``),
so the egress policy and DNS pinning of ADR-007 apply. Redirects are never followed.

Invariants of the port:

- ``put`` spools the stream into a private temporary file, enforcing ``max_bytes`` on the way, and then
  sends one ``PutObject`` with ``If-None-Match: *``: the object appears whole or not at all, and an existing
  key answers ``412`` (``StorageObjectExistsError``).
- ``move`` is ``CopyObject`` with ``If-None-Match: *`` and a ``HEAD`` of the target first (some servers
  ignore the condition on a copy), then ``DeleteObject`` of the source.
- ``local_path`` downloads into a private temporary file removed on exit.

Error messages never contain keys, the bucket or response bodies.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import shutil
import tempfile
import xml.etree.ElementTree as ElementTree
from collections.abc import AsyncIterable, AsyncIterator, Mapping
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

import httpx

from plummy.application.storage import (
    StorageArea,
    StorageKey,
    StorageLimitExceededError,
    StorageObjectExistsError,
    StorageObjectNotFoundError,
    StorageUnavailableError,
    StoredObject,
)
from plummy.core.network.errors import EgressDeniedError, ResponseTooLargeError
from plummy.core.network.policy import EgressPurpose
from plummy.infrastructure.http.safe_client import SafeHttpClient
from plummy.infrastructure.storage.sigv4 import EMPTY_SHA256, Credentials, canonical_query, sign

logger = logging.getLogger(__name__)

READ_CHUNK_BYTES: Final = 256 * 1024
# Objects are at most UPLOAD_MAX_BYTES (≤ 2 GiB) or derived JSON; the bound only stops a runaway stream.
MAX_OBJECT_BYTES: Final = 4 * 1024 * 1024 * 1024
MAX_LIST_BYTES: Final = 4 * 1024 * 1024
TIMEOUT: Final = httpx.Timeout(connect=10.0, read=300.0, write=300.0, pool=30.0)
_S3_NS: Final = "{http://s3.amazonaws.com/doc/2006-03-01/}"
_DIR_MODE: Final = 0o700


class S3Storage:
    def __init__(
        self,
        http: SafeHttpClient,
        *,
        endpoint_url: str,
        bucket: str,
        region: str,
        credentials: Credentials,
        prefix: str = "",
        temp_dir: Path | None = None,
    ) -> None:
        self._http = http
        self._endpoint = endpoint_url.rstrip("/")
        self._host = httpx.URL(self._endpoint).netloc.decode("ascii")
        self._bucket = bucket
        self._region = region
        self._credentials = credentials
        self._prefix = prefix
        self._temp_root = (temp_dir or Path(tempfile.gettempdir())) / "plummy-storage"

    # --- requests ---------------------------------------------------------------------------------------

    def _object_path(self, area: StorageArea, key: StorageKey) -> str:
        return f"/{self._bucket}/{self._object_key(area, key)}"

    def _object_key(self, area: StorageArea, key: StorageKey) -> str:
        return f"{self._prefix}{StorageArea(area).value}/{key.value[-2:]}/{key.value}"

    def _prepare(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        payload_sha256: str = EMPTY_SHA256,
    ) -> tuple[str, dict[str, str]]:
        query = canonical_query(params or {})
        url = f"{self._endpoint}{path}" + (f"?{query}" if query else "")
        signed = dict(headers or {})
        signed.update(
            sign(
                method=method,
                host=self._host,
                path=path,
                params=params or {},
                headers=headers or {},
                payload_sha256=payload_sha256,
                credentials=self._credentials,
                region=self._region,
                now=datetime.now(UTC),
            )
        )
        return url, signed

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        content: bytes | AsyncIterable[bytes] | None = None,
        payload_sha256: str = EMPTY_SHA256,
        max_response_bytes: int = MAX_LIST_BYTES,
    ) -> httpx.Response:
        url, signed = self._prepare(
            method, path, params=params, headers=headers, payload_sha256=payload_sha256
        )
        try:
            return await self._http.request(
                method,
                url,
                purpose=EgressPurpose.STORAGE,
                headers=signed,
                content=content,
                timeout=TIMEOUT,
                max_response_bytes=max_response_bytes,
            )
        except (httpx.HTTPError, EgressDeniedError, ResponseTooLargeError) as exc:
            logger.warning("storage request failed: %s %s", method, type(exc).__name__)
            raise StorageUnavailableError() from None

    @staticmethod
    def _unexpected(response: httpx.Response, operation: str) -> StorageUnavailableError:
        logger.warning("storage %s answered %d", operation, response.status_code)
        return StorageUnavailableError()

    # --- temporary files --------------------------------------------------------------------------------

    def _temp_dir(self) -> Path:
        self._temp_root.mkdir(mode=_DIR_MODE, exist_ok=True)
        if self._temp_root.is_symlink() or not self._temp_root.is_dir():
            raise StorageUnavailableError()
        os.chmod(self._temp_root, _DIR_MODE)
        return Path(tempfile.mkdtemp(dir=self._temp_root))

    # --- write ------------------------------------------------------------------------------------------

    async def put(
        self,
        area: StorageArea,
        key: StorageKey,
        data: AsyncIterable[bytes],
        *,
        max_bytes: int | None = None,
    ) -> StoredObject:
        if max_bytes is not None and max_bytes < 0:
            raise ValueError("max_bytes must not be negative")
        directory = await asyncio.to_thread(self._temp_dir)
        spool = directory / "object"
        try:
            size, digest = await _spool(data, spool, max_bytes)
            response = await self._send(
                "PUT",
                self._object_path(area, key),
                headers={
                    "Content-Length": str(size),
                    "Content-Type": "application/octet-stream",
                    "If-None-Match": "*",
                },
                content=_read_file(spool),
                payload_sha256=digest.hex(),
            )
        finally:
            await asyncio.to_thread(shutil.rmtree, directory, True)
        if response.status_code == 412:
            raise StorageObjectExistsError()
        if response.status_code != 200:
            raise self._unexpected(response, "put")
        return StoredObject(area=area, key=key, size_bytes=size, sha256=digest)

    # --- read -------------------------------------------------------------------------------------------

    async def open(self, area: StorageArea, key: StorageKey) -> AsyncIterator[bytes]:
        url, signed = self._prepare("GET", self._object_path(area, key))
        try:
            async with self._http.stream(
                "GET",
                url,
                purpose=EgressPurpose.STORAGE,
                headers=signed,
                timeout=TIMEOUT,
                max_response_bytes=MAX_OBJECT_BYTES,
            ) as response:
                if response.status_code == 404:
                    raise StorageObjectNotFoundError()
                if response.status_code != 200:
                    raise self._unexpected(response, "get")
                async for chunk in response.aiter_bytes(READ_CHUNK_BYTES):
                    yield chunk
        except (httpx.HTTPError, EgressDeniedError, ResponseTooLargeError) as exc:
            logger.warning("storage request failed: GET %s", type(exc).__name__)
            raise StorageUnavailableError() from None

    @asynccontextmanager
    async def local_path(self, area: StorageArea, key: StorageKey) -> AsyncIterator[Path]:
        directory = await asyncio.to_thread(self._temp_dir)
        try:
            path = directory / "object"
            fd = await asyncio.to_thread(os.open, path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                async for chunk in self.open(area, key):
                    await asyncio.to_thread(_write_all, fd, chunk)
            finally:
                await asyncio.to_thread(os.close, fd)
            await asyncio.to_thread(os.chmod, path, 0o400)
            yield path
        finally:
            await asyncio.to_thread(shutil.rmtree, directory, True)

    async def exists(self, area: StorageArea, key: StorageKey) -> bool:
        response = await self._send("HEAD", self._object_path(area, key))
        if response.status_code == 200:
            return True
        if response.status_code == 404:
            return False
        raise self._unexpected(response, "head")

    # --- move / delete ----------------------------------------------------------------------------------

    async def move(self, key: StorageKey, source: StorageArea, target: StorageArea) -> None:
        if await self.exists(target, key):
            raise StorageObjectExistsError()
        response = await self._send(
            "PUT",
            self._object_path(target, key),
            headers={"x-amz-copy-source": self._object_path(source, key), "If-None-Match": "*"},
        )
        if response.status_code == 404:
            raise StorageObjectNotFoundError()
        if response.status_code == 412:
            raise StorageObjectExistsError()
        if response.status_code != 200 or b"<Error>" in response.content:
            # CopyObject may answer 200 with an error document in the body.
            raise self._unexpected(response, "copy")
        await self.delete(source, key)

    async def delete(self, area: StorageArea, key: StorageKey) -> None:
        response = await self._send("DELETE", self._object_path(area, key))
        if response.status_code not in (200, 204, 404):
            raise self._unexpected(response, "delete")

    async def purge_expired(self, area: StorageArea, older_than: datetime) -> int:
        prefix = f"{self._prefix}{StorageArea(area).value}/"
        removed = 0
        token: str | None = None
        while True:
            params = {"list-type": "2", "prefix": prefix, "max-keys": "1000"}
            if token is not None:
                params["continuation-token"] = token
            response = await self._send("GET", f"/{self._bucket}", params=params)
            if response.status_code != 200:
                raise self._unexpected(response, "list")
            expired, token = _parse_listing(response.content, _own_key(prefix), older_than)
            for object_key in expired:
                delete = await self._send("DELETE", f"/{self._bucket}/{object_key}")
                if delete.status_code in (200, 204, 404):
                    removed += 1
                else:
                    raise self._unexpected(delete, "delete")
            if token is None:
                return removed

    async def check_bucket(self) -> None:
        """``HeadBucket`` with these credentials (no write)."""
        response = await self._send("HEAD", f"/{self._bucket}")
        if response.status_code != 200:
            raise self._unexpected(response, "head bucket")

    async def aclose(self) -> None:
        """The HTTP client belongs to the storage (one per process); closing is idempotent."""
        await self._http.aclose()


class S3StorageCheck:
    """Readiness: the bucket answers ``HeadBucket`` with these credentials (no write)."""

    name = "storage"

    def __init__(self, storage: S3Storage) -> None:
        self._storage = storage

    async def check(self) -> None:
        await self._storage.check_bucket()


# --- helpers ----------------------------------------------------------------------------------------------


async def _spool(data: AsyncIterable[bytes], path: Path, max_bytes: int | None) -> tuple[int, bytes]:
    digest = hashlib.sha256()
    size = 0
    fd = await asyncio.to_thread(os.open, path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        async for chunk in data:
            if not chunk:
                continue
            size += len(chunk)
            if max_bytes is not None and size > max_bytes:
                raise StorageLimitExceededError(max_bytes)
            digest.update(chunk)
            await asyncio.to_thread(_write_all, fd, chunk)
    finally:
        await asyncio.to_thread(os.close, fd)
    return size, digest.digest()


async def _read_file(path: Path) -> AsyncIterator[bytes]:
    fd = await asyncio.to_thread(os.open, path, os.O_RDONLY)
    try:
        while chunk := await asyncio.to_thread(os.read, fd, READ_CHUNK_BYTES):
            yield chunk
    finally:
        await asyncio.to_thread(os.close, fd)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _own_key(prefix: str) -> re.Pattern[str]:
    """Object keys of this layout below ``prefix``; anything else in the bucket is never touched."""
    return re.compile(re.escape(prefix) + r"[0-9a-f]{2}/[0-9a-f]{32}")


def _parse_listing(body: bytes, own: re.Pattern[str], older_than: datetime) -> tuple[list[str], str | None]:
    """Own keys last modified before ``older_than``, and the continuation token, if any."""
    try:
        root = ElementTree.fromstring(body)  # noqa: S314 - the configured storage service, size-limited
    except ElementTree.ParseError:
        raise StorageUnavailableError() from None
    expired: list[str] = []
    for item in root.iter(f"{_S3_NS}Contents"):
        key = item.findtext(f"{_S3_NS}Key")
        modified = item.findtext(f"{_S3_NS}LastModified")
        if not key or not modified or not own.fullmatch(key):
            continue
        with suppress(ValueError):
            when = datetime.fromisoformat(modified.replace("Z", "+00:00"))
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            if when < older_than:
                expired.append(key)
    truncated = (root.findtext(f"{_S3_NS}IsTruncated") or "").strip().lower() == "true"
    token = root.findtext(f"{_S3_NS}NextContinuationToken") if truncated else None
    return expired, token or None
