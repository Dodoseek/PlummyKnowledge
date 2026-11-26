"""Local filesystem implementation of ``StorageProvider`` (SECURITY_MODEL.md §9).

Layout: ``<root>/<area>/<last two key characters>/<key>``. Directories get mode 0700 and files 0600; nothing
is executable. Paths are built only from the area enum and a validated key, and every resolved path is checked
to stay inside the root. Files are written to a temporary name and published with ``link``, so a reader never
sees a partial file and an existing object is never overwritten. Symlinks are not followed.

Blocking file calls run in a worker thread so they do not stall the event loop.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import os
import secrets
from collections.abc import AsyncIterable, AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Final

from plummy.application.storage import (
    StorageArea,
    StorageKey,
    StorageLimitExceededError,
    StorageObjectExistsError,
    StorageObjectNotFoundError,
    StoredObject,
)

READ_CHUNK_BYTES: Final = 256 * 1024
_DIR_MODE: Final = 0o700
_FILE_MODE: Final = 0o600
_TEMP_PREFIX: Final = ".tmp-"
_OPEN_READ: Final = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
_OPEN_NEW: Final = (
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
)


class LocalFileStorage:
    def __init__(self, root: Path) -> None:
        self._root = root.resolve()

    @property
    def root(self) -> Path:
        return self._root

    # --- paths ------------------------------------------------------------------------------------------

    def _path(self, area: StorageArea, key: StorageKey) -> Path:
        path = self._root / StorageArea(area).value / key.value[-2:] / key.value
        # Defence in depth: the key format already excludes separators and dots.
        if not path.resolve(strict=False).is_relative_to(self._root):
            raise StorageObjectNotFoundError()
        return path

    def _ensure_parent(self, path: Path) -> None:
        """Create the area and shard directories (0700) below the existing root; refuse symlinked ones."""
        for directory in (path.parent.parent, path.parent):
            try:
                directory.mkdir(mode=_DIR_MODE)
            except FileExistsError:
                pass
            else:
                os.chmod(directory, _DIR_MODE)
            if directory.is_symlink() or not directory.is_dir():
                raise StorageObjectNotFoundError()

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
        target = self._path(area, key)
        await asyncio.to_thread(self._ensure_parent, target)
        temp = target.with_name(f"{_TEMP_PREFIX}{key.value}-{secrets.token_hex(4)}")
        fd = await asyncio.to_thread(os.open, temp, _OPEN_NEW, _FILE_MODE)
        digest = hashlib.sha256()
        size = 0
        try:
            async for chunk in data:
                if not chunk:
                    continue
                size += len(chunk)
                if max_bytes is not None and size > max_bytes:
                    raise StorageLimitExceededError(max_bytes)
                digest.update(chunk)
                await asyncio.to_thread(_write_all, fd, chunk)
            await asyncio.to_thread(os.fsync, fd)
            await asyncio.to_thread(os.close, fd)
            fd = -1
            try:
                await asyncio.to_thread(os.link, temp, target)
            except FileExistsError:
                raise StorageObjectExistsError() from None
        finally:
            if fd >= 0:
                await asyncio.to_thread(os.close, fd)
            await asyncio.to_thread(_unlink_quietly, temp)
        return StoredObject(area=area, key=key, size_bytes=size, sha256=digest.digest())

    # --- read -------------------------------------------------------------------------------------------

    async def open(self, area: StorageArea, key: StorageKey) -> AsyncIterator[bytes]:
        path = self._path(area, key)
        fd = await asyncio.to_thread(_open_existing, path)
        try:
            while chunk := await asyncio.to_thread(os.read, fd, READ_CHUNK_BYTES):
                yield chunk
        finally:
            await asyncio.to_thread(os.close, fd)

    @asynccontextmanager
    async def local_path(self, area: StorageArea, key: StorageKey) -> AsyncIterator[Path]:
        path = self._path(area, key)
        fd = await asyncio.to_thread(_open_existing, path)  # existence and "not a symlink" check
        await asyncio.to_thread(os.close, fd)
        yield path

    async def exists(self, area: StorageArea, key: StorageKey) -> bool:
        try:
            path = self._path(area, key)
        except StorageObjectNotFoundError:
            return False
        return await asyncio.to_thread(_is_regular_file, path)

    # --- move / delete ----------------------------------------------------------------------------------

    async def move(self, key: StorageKey, source: StorageArea, target: StorageArea) -> None:
        src = self._path(source, key)
        dst = self._path(target, key)
        await asyncio.to_thread(self._ensure_parent, dst)
        if not await asyncio.to_thread(_is_regular_file, src):
            raise StorageObjectNotFoundError()
        try:
            await asyncio.to_thread(os.link, src, dst)
        except FileExistsError:
            raise StorageObjectExistsError() from None
        await asyncio.to_thread(_unlink_quietly, src)

    async def delete(self, area: StorageArea, key: StorageKey) -> None:
        await asyncio.to_thread(_unlink_quietly, self._path(area, key))

    async def purge_expired(self, area: StorageArea, older_than: datetime) -> int:
        return await asyncio.to_thread(
            self._purge, self._root / StorageArea(area).value, older_than.timestamp()
        )

    @staticmethod
    def _purge(directory: Path, cutoff: float) -> int:
        removed = 0
        if directory.is_symlink() or not directory.is_dir():
            return 0
        for shard in directory.iterdir():
            if shard.is_symlink() or not shard.is_dir():
                continue
            for entry in shard.iterdir():
                try:
                    stat = entry.lstat()
                except FileNotFoundError:
                    continue
                if stat.st_mtime < cutoff:
                    _unlink_quietly(entry)
                    removed += 1
        return removed

    async def aclose(self) -> None:
        """Nothing to release; present so both storage backends close the same way."""


class StorageCheck:
    """Readiness: the storage root exists and is writable (a probe file is created and removed)."""

    name = "storage"

    def __init__(self, storage: LocalFileStorage) -> None:
        self._storage = storage

    async def check(self) -> None:
        await asyncio.to_thread(self._probe)

    def _probe(self) -> None:
        probe = self._storage.root / f".probe-{secrets.token_hex(8)}"
        fd = os.open(probe, _OPEN_NEW, _FILE_MODE)
        try:
            os.write(fd, b"ok")
        finally:
            os.close(fd)
            _unlink_quietly(probe)


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _open_existing(path: Path) -> int:
    try:
        return os.open(path, _OPEN_READ)
    except FileNotFoundError:
        raise StorageObjectNotFoundError() from None
    except OSError as exc:
        if exc.errno == errno.ELOOP:  # a symlink where a file was expected
            raise StorageObjectNotFoundError() from None
        raise


def _is_regular_file(path: Path) -> bool:
    try:
        return path.lstat().st_mode & 0o170000 == 0o100000
    except FileNotFoundError:
        return False


def _unlink_quietly(path: Path) -> None:
    with suppress(FileNotFoundError):
        os.unlink(path)
