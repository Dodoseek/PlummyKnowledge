"""``maintenance export-objects`` / ``import-objects``: stored objects as a tar stream (ADR-023 §6).

Exported from local storage and imported into the S3 server of the dev stack (``S3_TEST_URL``; the S3 part
is skipped without it), so a backup moves between storage backends. The import accepts storage keys only.
"""

from __future__ import annotations

import hashlib
import io
import secrets
import tarfile
from pathlib import Path

import pytest

from plummy.application.maintenance.objects import ExportObjects, ImportObjects
from plummy.application.storage import StorageArea, StorageKey
from plummy.infrastructure.storage.local import LocalFileStorage
from tests.integration.test_storage_contract import bucket, make_s3, read_all, stream  # noqa: F401 - fixture

pytestmark = pytest.mark.integration

Q, D = StorageArea.QUARANTINE, StorageArea.DOCUMENTS


def local(tmp_path: Path, name: str) -> LocalFileStorage:
    root = tmp_path / name
    root.mkdir(mode=0o700)
    return LocalFileStorage(root)


@pytest.mark.usefixtures("bucket")
async def test_round_trip_from_local_to_s3_and_import_is_repeatable(tmp_path: Path) -> None:
    source = local(tmp_path, "source")
    objects = {(D, StorageKey.new()): secrets.token_bytes(200_000), (Q, StorageKey.new()): b"pending upload"}
    for (area, key), data in objects.items():
        await source.put(area, key, stream(data))
    archive = io.BytesIO()
    exported = await ExportObjects(source).execute(archive)
    assert exported.stored == {"documents": 1, "quarantine": 1}

    target = make_s3(tmp_path, prefix=f"t{secrets.token_hex(6)}/")
    archive.seek(0)
    imported = await ImportObjects(target).execute(archive)
    assert imported.stored == {"documents": 1, "quarantine": 1}
    assert imported.rejected == 0
    for (area, key), data in objects.items():
        assert hashlib.sha256(await read_all(target, area, key)).digest() == hashlib.sha256(data).digest()

    archive.seek(0)
    again = await ImportObjects(target).execute(archive)
    assert not again.stored
    assert again.kept == {"documents": 1, "quarantine": 1}
    await target.aclose()


async def test_the_archive_holds_storage_keys_only(tmp_path: Path) -> None:
    source = local(tmp_path, "source")
    key = StorageKey.new()
    await source.put(D, key, stream(b"x"))
    archive = io.BytesIO()
    await ExportObjects(source).execute(archive)
    archive.seek(0)
    with tarfile.open(fileobj=archive) as tar:
        members = tar.getmembers()
    assert [(m.name, m.mode, m.uid, m.uname) for m in members] == [(f"documents/{key.value}", 0o600, 0, "")]


def _archive(*entries: tarfile.TarInfo, data: bytes = b"payload") -> io.BytesIO:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        for entry in entries:
            if entry.isreg():
                entry.size = len(data)
                tar.addfile(entry, io.BytesIO(data))
            else:
                tar.addfile(entry)
    buffer.seek(0)
    return buffer


def _entry(name: str, kind: bytes = tarfile.REGTYPE, link: str = "") -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.type, info.linkname = kind, link
    return info


async def test_import_rejects_anything_but_objects(tmp_path: Path) -> None:
    target = local(tmp_path, "target")
    good = StorageKey.new()
    archive = _archive(
        _entry(f"documents/{good.value}"),
        _entry("../../etc/passwd"),
        _entry(f"documents/../{StorageKey.new().value}"),
        _entry(f"other/{StorageKey.new().value}"),
        _entry(f"documents/{StorageKey.new().value}.pdf"),
        _entry("documents/report.pdf"),
        _entry(f"quarantine/{StorageKey.new().value}", tarfile.SYMTYPE, "/etc/passwd"),
        _entry(f"quarantine/{StorageKey.new().value}", tarfile.DIRTYPE),
    )
    report = await ImportObjects(target).execute(archive)
    assert report.stored == {"documents": 1}
    assert report.rejected == 7
    assert [key async for key in target.keys(D)] == [good]
    assert [key async for key in target.keys(Q)] == []
    assert not (tmp_path / "etc").exists()
