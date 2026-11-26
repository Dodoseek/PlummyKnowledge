"""Stored objects to and from a tar stream, through ``StorageProvider`` (ADR-023 §6).

Backups of documents go through the storage port instead of copying the storage's own files: the same
commands work for local storage, the bundled S3 server and an external S3, and the server-side metadata the
S3 server keeps outside the object (ETag, content type, checksums) is rebuilt by ``put`` on restore.

The archive holds ``documents/<key>`` and ``quarantine/<key>`` entries only. Import accepts nothing else:
no other names, directories, links or special files; existing objects are kept (``put`` never overwrites),
so an import can be repeated.
"""

from __future__ import annotations

import asyncio
import re
import tarfile
from collections import Counter
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Final

from plummy.application.storage import (
    StorageArea,
    StorageKey,
    StorageObjectExistsError,
    StorageObjectNotFoundError,
    StorageProvider,
)

ENTRY: Final = re.compile(r"(documents|quarantine)/([0-9a-f]{32})")
CHUNK: Final = 1024 * 1024


@dataclass(slots=True)
class ObjectsReport:
    stored: Counter[str] = field(default_factory=Counter)
    kept: Counter[str] = field(default_factory=Counter)  # already present: never overwritten
    rejected: int = 0  # entries that are not objects of this layout


class ExportObjects:
    def __init__(self, storage: StorageProvider) -> None:
        self._storage = storage

    async def execute(self, sink: IO[bytes]) -> ObjectsReport:
        report = ObjectsReport()
        with tarfile.open(fileobj=sink, mode="w|", format=tarfile.PAX_FORMAT) as archive:
            for area in StorageArea:
                async for key in self._storage.keys(area):
                    try:
                        async with self._storage.local_path(area, key) as path:
                            await asyncio.to_thread(_add, archive, path, f"{area.value}/{key.value}")
                    except StorageObjectNotFoundError:
                        continue  # deleted while the backup ran
                    report.stored[area.value] += 1
        return report


def _add(archive: tarfile.TarFile, path: Path, name: str) -> None:
    """A plain entry: no owner, mode 0600; the name is the storage key, never a user's file name."""
    stat = path.stat()
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime = stat.st_size, 0o600, int(stat.st_mtime)
    with path.open("rb") as content:
        archive.addfile(info, content)


class ImportObjects:
    def __init__(self, storage: StorageProvider) -> None:
        self._storage = storage

    async def execute(self, source: IO[bytes]) -> ObjectsReport:
        report = ObjectsReport()
        with tarfile.open(fileobj=source, mode="r|") as archive:
            for member in archive:
                match = ENTRY.fullmatch(member.name)
                if match is None or not member.isreg():
                    report.rejected += 1
                    continue
                content = archive.extractfile(member)
                if content is None:  # pragma: no cover - a regular file always has content
                    report.rejected += 1
                    continue
                area, key = StorageArea(match[1]), StorageKey(match[2])
                try:
                    await self._storage.put(area, key, _chunks(content))
                except StorageObjectExistsError:
                    report.kept[area.value] += 1
                    continue
                report.stored[area.value] += 1
        return report


async def _chunks(content: IO[bytes]) -> AsyncIterator[bytes]:
    while chunk := content.read(CHUNK):
        yield chunk
