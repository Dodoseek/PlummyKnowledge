"""One contract for both ``StorageProvider`` backends (ARCHITECTURE.md §8.5, ADR-022).

The local backend runs on a temporary directory; the S3 backend on the real S3-compatible server of the dev
stack (versitygw, ``S3_TEST_URL``; skipped without it). Each S3 test gets its own prefix in one bucket.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import socket
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest

from plummy.application.storage import (
    StorageArea,
    StorageKey,
    StorageLimitExceededError,
    StorageObjectExistsError,
    StorageObjectNotFoundError,
    StorageUnavailableError,
)
from plummy.core.instance_config import build_egress_policy, parse_instance_config
from plummy.infrastructure.http.safe_client import SafeHttpClient
from plummy.infrastructure.storage.local import LocalFileStorage
from plummy.infrastructure.storage.s3 import S3Storage, S3StorageCheck
from plummy.infrastructure.storage.sigv4 import EMPTY_SHA256, Credentials, sign

Q = StorageArea.QUARANTINE
D = StorageArea.DOCUMENTS
S3_URL = os.environ.get("S3_TEST_URL")
S3_CREDENTIALS = Credentials(
    os.environ.get("S3_TEST_ACCESS_KEY_ID", ""), os.environ.get("S3_TEST_SECRET_ACCESS_KEY", "")
)
BUCKET = f"plummy-test-{secrets.token_hex(4)}"

Storage = LocalFileStorage | S3Storage


async def stream(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


async def failing(after: bytes) -> AsyncIterator[bytes]:
    yield after
    raise RuntimeError("source broke")


async def read_all(storage: Storage, area: StorageArea, key: StorageKey) -> bytes:
    return b"".join([chunk async for chunk in storage.open(area, key)])


def s3_policy_config(url: str) -> str:
    host = urlsplit(url).hostname
    assert host is not None
    address = socket.gethostbyname(host)
    return (
        f'[[network.allowlist]]\nname = "test-s3"\nurl = "{url}"\npurposes = ["storage"]\n'
        f'allowed_cidrs = ["{address}/32"]\n'
    )


def root_request(method: str, path: str) -> int:
    """Bucket administration with the server's root key (the application never creates buckets)."""
    assert S3_URL is not None
    host = urlsplit(S3_URL).netloc
    headers = sign(
        method=method,
        host=host,
        path=path,
        params={},
        headers={},
        payload_sha256=EMPTY_SHA256,
        credentials=S3_CREDENTIALS,
        region="us-east-1",
        now=datetime.now(UTC),
    )
    with httpx.Client() as client:
        return client.request(method, f"{S3_URL}{path}", headers=headers).status_code


def make_s3(
    tmp_path: Path, *, prefix: str, credentials: Credentials = S3_CREDENTIALS, bucket: str = BUCKET
) -> S3Storage:
    assert S3_URL is not None
    http = SafeHttpClient(build_egress_policy(parse_instance_config(s3_policy_config(S3_URL).encode())))
    return S3Storage(
        http,
        endpoint_url=S3_URL,
        bucket=bucket,
        region="us-east-1",
        credentials=credentials,
        prefix=prefix,
        temp_dir=tmp_path,
    )


@pytest.fixture(scope="module")
def bucket() -> str:
    if S3_URL is None:
        pytest.skip("S3_TEST_URL is not set")
    assert root_request("PUT", f"/{BUCKET}") == 200
    return BUCKET


@pytest.fixture(params=["local", "s3"])
async def storage(request: pytest.FixtureRequest, tmp_path: Path) -> AsyncIterator[Storage]:
    if request.param == "local":
        root = tmp_path / "storage"
        root.mkdir(mode=0o700)
        yield LocalFileStorage(root)
        return
    request.getfixturevalue("bucket")
    s3 = make_s3(tmp_path, prefix=f"t{secrets.token_hex(6)}/")
    try:
        yield s3
    finally:
        await s3.aclose()


# --- the contract -------------------------------------------------------------------------------------------


async def test_put_and_open_round_trip(storage: Storage) -> None:
    key = StorageKey.new()
    data = [secrets.token_bytes(300_000), b"tail"]
    stored = await storage.put(Q, key, stream(*data))
    assert stored.size_bytes == 300_004
    assert stored.sha256 == hashlib.sha256(b"".join(data)).digest()
    assert await read_all(storage, Q, key) == b"".join(data)
    assert await storage.exists(Q, key)
    assert not await storage.exists(D, key)


async def test_empty_object(storage: Storage) -> None:
    key = StorageKey.new()
    stored = await storage.put(D, key, stream())
    assert stored.size_bytes == 0
    assert await read_all(storage, D, key) == b""


async def test_limit_is_enforced_and_nothing_is_stored(storage: Storage) -> None:
    key = StorageKey.new()
    with pytest.raises(StorageLimitExceededError):
        await storage.put(Q, key, stream(b"x" * 600, b"y" * 600), max_bytes=1000)
    assert not await storage.exists(Q, key)
    exact = StorageKey.new()
    await storage.put(Q, exact, stream(b"z" * 1000), max_bytes=1000)
    assert await storage.exists(Q, exact)


async def test_a_broken_source_leaves_nothing(storage: Storage) -> None:
    key = StorageKey.new()
    with pytest.raises(RuntimeError, match="source broke"):
        await storage.put(Q, key, failing(b"partial"))
    assert not await storage.exists(Q, key)


async def test_put_never_overwrites(storage: Storage) -> None:
    key = StorageKey.new()
    await storage.put(Q, key, stream(b"first"))
    with pytest.raises(StorageObjectExistsError):
        await storage.put(Q, key, stream(b"second"))
    assert await read_all(storage, Q, key) == b"first"


async def test_missing_object(storage: Storage) -> None:
    key = StorageKey.new()
    with pytest.raises(StorageObjectNotFoundError):
        await read_all(storage, D, key)
    with pytest.raises(StorageObjectNotFoundError):
        async with storage.local_path(D, key):
            pass
    assert not await storage.exists(D, key)
    await storage.delete(D, key)  # not an error


async def test_local_path_has_the_content(storage: Storage) -> None:
    key = StorageKey.new()
    await storage.put(D, key, stream(b"%PDF-1.7 content"))
    async with storage.local_path(D, key) as path:
        assert path.read_bytes() == b"%PDF-1.7 content"
    if isinstance(storage, S3Storage):
        assert not path.exists()  # the downloaded copy is removed


async def test_move_between_areas(storage: Storage) -> None:
    key = StorageKey.new()
    await storage.put(Q, key, stream(b"moved"))
    await storage.move(key, Q, D)
    assert not await storage.exists(Q, key)
    assert await read_all(storage, D, key) == b"moved"


async def test_move_refuses_to_overwrite_and_reports_a_missing_source(storage: Storage) -> None:
    key = StorageKey.new()
    await storage.put(Q, key, stream(b"new"))
    await storage.put(D, key, stream(b"already there"))
    with pytest.raises(StorageObjectExistsError):
        await storage.move(key, Q, D)
    assert await read_all(storage, D, key) == b"already there"
    assert await storage.exists(Q, key)  # the source is kept for the caller to decide
    with pytest.raises(StorageObjectNotFoundError):
        await storage.move(StorageKey.new(), Q, D)


async def test_delete_is_idempotent(storage: Storage) -> None:
    key = StorageKey.new()
    await storage.put(D, key, stream(b"x"))
    await storage.delete(D, key)
    await storage.delete(D, key)
    assert not await storage.exists(D, key)


async def test_purge_expired_touches_only_its_area(storage: Storage) -> None:
    quarantined, document = StorageKey.new(), StorageKey.new()
    await storage.put(Q, quarantined, stream(b"q"))
    await storage.put(D, document, stream(b"d"))
    assert await storage.purge_expired(Q, datetime.now(UTC) - timedelta(hours=1)) == 0
    assert await storage.exists(Q, quarantined)
    assert await storage.purge_expired(Q, datetime.now(UTC) + timedelta(minutes=5)) == 1
    assert not await storage.exists(Q, quarantined)
    assert await storage.exists(D, document)


# --- S3 only ------------------------------------------------------------------------------------------------


async def test_s3_layout_and_foreign_objects(bucket: str, tmp_path: Path) -> None:
    prefix = f"t{secrets.token_hex(6)}/"
    storage = make_s3(tmp_path, prefix=prefix)
    key = StorageKey.new()
    await storage.put(Q, key, stream(b"x"))
    # the object sits at the documented layout, readable by its full key with the root credentials
    assert root_request("HEAD", f"/{bucket}/{prefix}quarantine/{key.value[-2:]}/{key.value}") == 200
    # a foreign object under the same area prefix is never purged
    foreign = StorageKey.new()
    await make_s3(tmp_path, prefix=f"{prefix}quarantine/").put(Q, foreign, stream(b"foreign"))
    removed = await storage.purge_expired(Q, datetime.now(UTC) + timedelta(minutes=5))
    assert removed == 1
    nested = f"/{bucket}/{prefix}quarantine/quarantine/{foreign.value[-2:]}/{foreign.value}"
    assert root_request("HEAD", nested) == 200
    await storage.aclose()


async def test_purge_follows_continuation_pages(bucket: str, tmp_path: Path) -> None:
    storage = make_s3(tmp_path, prefix=f"t{secrets.token_hex(6)}/")
    keys = [StorageKey.new() for _ in range(5)]
    for key in keys:
        await storage.put(Q, key, stream(b"x"))
    original = storage._send

    async def small_pages(method: str, path: str, **kwargs: object) -> httpx.Response:
        params = kwargs.get("params")
        if isinstance(params, dict) and "list-type" in params:
            kwargs["params"] = {**params, "max-keys": "2"}
        return await original(method, path, **kwargs)  # type: ignore[arg-type]

    storage._send = small_pages  # type: ignore[method-assign]
    assert await storage.purge_expired(Q, datetime.now(UTC) + timedelta(minutes=5)) == 5
    for key in keys:
        assert not await storage.exists(Q, key)
    await storage.aclose()


async def test_readiness_checks_the_bucket(bucket: str, tmp_path: Path) -> None:
    good = make_s3(tmp_path, prefix="")
    await S3StorageCheck(good).check()
    await good.aclose()
    missing = make_s3(tmp_path, prefix="", bucket=f"plummy-missing-{secrets.token_hex(4)}")
    with pytest.raises(StorageUnavailableError):
        await S3StorageCheck(missing).check()
    await missing.aclose()


async def test_wrong_credentials_are_unavailable(bucket: str, tmp_path: Path) -> None:
    wrong = make_s3(
        tmp_path, prefix="", credentials=Credentials(S3_CREDENTIALS.access_key_id, "wrong-secret")
    )
    with pytest.raises(StorageUnavailableError):
        await wrong.put(Q, StorageKey.new(), stream(b"x"))
    with pytest.raises(StorageUnavailableError):
        await S3StorageCheck(wrong).check()
    await wrong.aclose()
