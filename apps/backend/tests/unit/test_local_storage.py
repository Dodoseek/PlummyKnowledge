"""Local file storage (ARCHITECTURE.md §8.5, SECURITY_MODEL.md §9): keys, quarantine, limits, no escape."""

from __future__ import annotations

import hashlib
import os
import stat
import time
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from plummy.application.storage import (
    StorageArea,
    StorageKey,
    StorageLimitExceededError,
    StorageObjectExistsError,
    StorageObjectNotFoundError,
)
from plummy.infrastructure.storage.local import LocalFileStorage, StorageCheck

Q = StorageArea.QUARANTINE
D = StorageArea.DOCUMENTS


async def stream(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


async def read_all(storage: LocalFileStorage, area: StorageArea, key: StorageKey) -> bytes:
    return b"".join([chunk async for chunk in storage.open(area, key)])


@pytest.fixture
def storage(tmp_path: Path) -> LocalFileStorage:
    root = tmp_path / "storage"
    root.mkdir(mode=0o700)
    return LocalFileStorage(root)


def all_files(root: Path) -> list[Path]:
    return [path for path in root.rglob("*") if path.is_file()]


# --- keys -------------------------------------------------------------------------------------------------


def test_new_keys_are_random_hex_and_unique() -> None:
    keys = {StorageKey.new().value for _ in range(200)}
    assert len(keys) == 200
    assert all(len(key) == 32 and set(key) <= set("0123456789abcdef") for key in keys)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "../../etc/passwd",
        "..",
        "a" * 31,
        "a" * 33,
        "A" * 32,  # upper case is not produced by the server
        "0123456789abcdef0123456789abcde/",
        "0123456789abcdef0123456789abcdef\x00",
        "report.pdf",
    ],
)
def test_keys_from_outside_the_server_format_are_rejected(value: str) -> None:
    with pytest.raises(ValueError, match="invalid storage key"):
        StorageKey(value)


# --- put / open ---------------------------------------------------------------------------------------------


async def test_put_streams_and_reports_size_and_sha256(storage: LocalFileStorage) -> None:
    key = StorageKey.new()
    stored = await storage.put(Q, key, stream(b"hello ", b"", b"world"))
    assert stored.size_bytes == 11
    assert stored.sha256 == hashlib.sha256(b"hello world").digest()
    assert await read_all(storage, Q, key) == b"hello world"
    assert await storage.exists(Q, key)
    assert not await storage.exists(D, key)


async def test_empty_object_is_allowed(storage: LocalFileStorage) -> None:
    key = StorageKey.new()
    stored = await storage.put(D, key, stream())
    assert (stored.size_bytes, await read_all(storage, D, key)) == (0, b"")


async def test_files_and_directories_are_private(storage: LocalFileStorage) -> None:
    key = StorageKey.new()
    await storage.put(Q, key, stream(b"x"))
    path = storage.root / "quarantine" / key.value[-2:] / key.value
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.parent.parent.stat().st_mode) == 0o700


async def test_layout_uses_only_the_server_key(storage: LocalFileStorage) -> None:
    key = StorageKey.new()
    await storage.put(D, key, stream(b"x"))
    assert [p.relative_to(storage.root).as_posix() for p in all_files(storage.root)] == [
        f"documents/{key.value[-2:]}/{key.value}"
    ]


@pytest.mark.parametrize(("limit", "chunks"), [(4, (b"12", b"345")), (0, (b"1",))])
async def test_size_limit_is_enforced_while_streaming(
    storage: LocalFileStorage, limit: int, chunks: tuple[bytes, ...]
) -> None:
    consumed: list[bytes] = []

    async def source() -> AsyncIterator[bytes]:
        for chunk in (*chunks, b"never read"):
            consumed.append(chunk)
            yield chunk

    key = StorageKey.new()
    with pytest.raises(StorageLimitExceededError) as exc_info:
        await storage.put(Q, key, source(), max_bytes=limit)
    assert exc_info.value.limit == limit
    assert b"never read" not in consumed  # stops at the first chunk over the limit
    assert not await storage.exists(Q, key)
    assert all_files(storage.root) == []  # no partial or temporary file is left


async def test_exactly_the_limit_is_accepted(storage: LocalFileStorage) -> None:
    stored = await storage.put(Q, StorageKey.new(), stream(b"1234"), max_bytes=4)
    assert stored.size_bytes == 4


async def test_failing_source_leaves_nothing_behind(storage: LocalFileStorage) -> None:
    async def broken() -> AsyncIterator[bytes]:
        yield b"partial"
        raise ConnectionResetError("client went away")

    key = StorageKey.new()
    with pytest.raises(ConnectionResetError):
        await storage.put(Q, key, broken())
    assert all_files(storage.root) == []


async def test_put_never_overwrites(storage: LocalFileStorage) -> None:
    key = StorageKey.new()
    await storage.put(D, key, stream(b"original"))
    with pytest.raises(StorageObjectExistsError):
        await storage.put(D, key, stream(b"replacement"))
    assert await read_all(storage, D, key) == b"original"
    assert len(all_files(storage.root)) == 1


async def test_missing_object(storage: LocalFileStorage) -> None:
    key = StorageKey.new()
    with pytest.raises(StorageObjectNotFoundError):
        await read_all(storage, D, key)
    with pytest.raises(StorageObjectNotFoundError):
        async with storage.local_path(D, key):
            pass
    assert not await storage.exists(D, key)


async def test_symlinks_are_not_followed(storage: LocalFileStorage, tmp_path: Path) -> None:
    secret = tmp_path / "outside.txt"
    secret.write_bytes(b"outside the storage root")
    key = StorageKey.new()
    shard = storage.root / "documents" / key.value[-2:]
    shard.mkdir(parents=True)
    (shard / key.value).symlink_to(secret)

    with pytest.raises(StorageObjectNotFoundError):
        await read_all(storage, D, key)
    assert not await storage.exists(D, key)
    with pytest.raises(StorageObjectNotFoundError):
        await storage.move(key, D, Q)


async def test_symlinked_shard_directory_is_refused(storage: LocalFileStorage, tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    key = StorageKey.new()
    (storage.root / "quarantine").mkdir()
    (storage.root / "quarantine" / key.value[-2:]).symlink_to(outside, target_is_directory=True)
    with pytest.raises(StorageObjectNotFoundError):
        await storage.put(Q, key, stream(b"x"))
    assert list(outside.iterdir()) == []


async def test_local_path_points_to_the_content(storage: LocalFileStorage) -> None:
    key = StorageKey.new()
    await storage.put(D, key, stream(b"%PDF-1.7"))
    async with storage.local_path(D, key) as path:
        assert path.read_bytes() == b"%PDF-1.7"
        assert path.is_relative_to(storage.root)


# --- move / delete / purge ----------------------------------------------------------------------------------


async def test_move_from_quarantine_to_documents(storage: LocalFileStorage) -> None:
    key = StorageKey.new()
    await storage.put(Q, key, stream(b"data"))
    await storage.move(key, Q, D)
    assert not await storage.exists(Q, key)
    assert await read_all(storage, D, key) == b"data"


async def test_move_refuses_to_overwrite_and_reports_missing(storage: LocalFileStorage) -> None:
    key = StorageKey.new()
    await storage.put(Q, key, stream(b"new"))
    await storage.put(D, key, stream(b"old"))
    with pytest.raises(StorageObjectExistsError):
        await storage.move(key, Q, D)
    assert await read_all(storage, D, key) == b"old"
    assert await read_all(storage, Q, key) == b"new"
    with pytest.raises(StorageObjectNotFoundError):
        await storage.move(StorageKey.new(), Q, D)


async def test_delete_is_idempotent(storage: LocalFileStorage) -> None:
    key = StorageKey.new()
    await storage.put(D, key, stream(b"x"))
    await storage.delete(D, key)
    await storage.delete(D, key)
    assert not await storage.exists(D, key)


async def test_purge_expired_removes_only_old_files_of_the_area(storage: LocalFileStorage) -> None:
    old, fresh, document = StorageKey.new(), StorageKey.new(), StorageKey.new()
    for area, key in ((Q, old), (Q, fresh), (D, document)):
        await storage.put(area, key, stream(b"x"))
    hour_ago = time.time() - 3600
    for path in all_files(storage.root):
        if path.name in (old.value, document.value):
            os.utime(path, (hour_ago, hour_ago))
    # a temporary file abandoned by a crashed writer is purged as well
    stale_temp = storage.root / "quarantine" / old.value[-2:] / f".tmp-{old.value}-dead"
    stale_temp.write_bytes(b"partial")
    os.utime(stale_temp, (hour_ago, hour_ago))

    removed = await storage.purge_expired(Q, datetime.now(UTC) - timedelta(minutes=30))

    assert removed == 2
    assert not await storage.exists(Q, old)
    assert await storage.exists(Q, fresh)
    assert await storage.exists(D, document)  # other areas are untouched
    assert not stale_temp.exists()


async def test_purge_of_an_empty_area(storage: LocalFileStorage) -> None:
    assert await storage.purge_expired(Q, datetime.now(UTC)) == 0


# --- readiness ----------------------------------------------------------------------------------------------


async def test_storage_check_probes_the_root(storage: LocalFileStorage, tmp_path: Path) -> None:
    await StorageCheck(storage).check()
    assert all_files(storage.root) == []
    with pytest.raises(FileNotFoundError):
        await StorageCheck(LocalFileStorage(tmp_path / "missing")).check()
