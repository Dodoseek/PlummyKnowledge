"""S3 storage pieces without a real server (ADR-022): SigV4, settings, listing, error mapping, egress."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from plummy.application.storage import (
    StorageArea,
    StorageKey,
    StorageObjectExistsError,
    StorageObjectNotFoundError,
    StorageUnavailableError,
)
from plummy.composition import check_storage_configuration, create_storage
from plummy.core.config import ConfigurationError, StorageBackend
from plummy.core.instance_config import build_egress_policy, parse_instance_config
from plummy.core.logging import configure_logging
from plummy.infrastructure.http.safe_client import SafeHttpClient
from plummy.infrastructure.storage.s3 import S3Storage, S3StorageCheck, _own_key, _parse_listing
from plummy.infrastructure.storage.sigv4 import EMPTY_SHA256, Credentials, canonical_query, sign
from tests.conftest import make_settings
from tests.fake_http import ReceivedRequest, Reply, serve

# The published example credentials of the AWS SigV4 documentation, not a real key.
AWS_KEY_ID = "AKIAIOSFODNN7EXAMPLE"  # secret-scan: allow
AWS_SECRET = "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY"  # secret-scan: allow
AWS = Credentials(AWS_KEY_ID, AWS_SECRET)
AWS_DATE = datetime(2013, 5, 24, tzinfo=UTC)
AWS_HOST = "examplebucket.s3.amazonaws.com"
SECRET = "unit-test-secret-key-never-logged"


async def stream(*chunks: bytes) -> AsyncIterator[bytes]:
    for chunk in chunks:
        yield chunk


# --- SigV4: the examples of the AWS S3 documentation (signature calculation for the Authorization header) ---


def signature(headers: dict[str, str]) -> str:
    return headers["Authorization"].rsplit("Signature=", 1)[1]


def test_get_object_example() -> None:
    headers = sign(
        method="GET",
        host=AWS_HOST,
        path="/test.txt",
        params={},
        headers={"Range": "bytes=0-9"},
        payload_sha256=EMPTY_SHA256,
        credentials=AWS,
        region="us-east-1",
        now=AWS_DATE,
    )
    assert signature(headers) == "f0e8bdb87c964420e857bd35b5d6ed310bd44f0170aba48dd91039c6036bdb41"
    assert headers["Authorization"].startswith(
        f"AWS4-HMAC-SHA256 Credential={AWS_KEY_ID}/20130524/us-east-1/s3/aws4_request, "
        "SignedHeaders=host;range;x-amz-content-sha256;x-amz-date, "
    )
    assert headers["x-amz-date"] == "20130524T000000Z"


def test_put_object_example() -> None:
    headers = sign(
        method="PUT",
        host=AWS_HOST,
        path="/test$file.text",
        params={},
        headers={"Date": "Fri, 24 May 2013 00:00:00 GMT", "x-amz-storage-class": "REDUCED_REDUNDANCY"},
        payload_sha256="44ce7dd67c959e0d3524ffac1771dfbba87d2b6b4b4e99e42034a8b803f8b072",
        credentials=AWS,
        region="us-east-1",
        now=AWS_DATE,
    )
    assert signature(headers) == "98ad721746da40c64f1a55b78f14c238d841ea1380cd77a1b5971af0ece108bd"


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"lifecycle": ""}, "fea454ca298b7da1c68078a5d1bdbfbbe0d65c699e0f91ac7a200a0136783543"),
        (
            {"max-keys": "2", "prefix": "J"},
            "34b48302e7b5fa45bde8084f4b7868a86f0a534bc59db6670ed5711ef69dc6f7",
        ),
    ],
)
def test_bucket_examples(params: dict[str, str], expected: str) -> None:
    headers = sign(
        method="GET",
        host=AWS_HOST,
        path="/",
        params=params,
        headers={},
        payload_sha256=EMPTY_SHA256,
        credentials=AWS,
        region="us-east-1",
        now=AWS_DATE,
    )
    assert signature(headers) == expected


def test_query_is_encoded_and_sorted() -> None:
    assert canonical_query({"prefix": "a/b c", "list-type": "2", "token": "x+y="}) == (
        "list-type=2&prefix=a%2Fb%20c&token=x%2By%3D"
    )


def test_credentials_repr_hides_the_secret() -> None:
    assert SECRET not in repr(Credentials("key", SECRET))


# --- settings -----------------------------------------------------------------------------------------------


S3_SETTINGS: dict[str, Any] = {
    "storage_backend": StorageBackend.S3,
    "s3_endpoint_url": "https://s3.example.com",
    "s3_bucket": "plummy-docs",
    "s3_access_key_id": "app",
    "s3_secret_access_key": SECRET,
}


def test_s3_settings_are_accepted_and_the_secret_stays_hidden() -> None:
    settings = make_settings(**S3_SETTINGS, s3_prefix="prod/", s3_region="eu-central-1")
    assert settings.s3_endpoint_url == "https://s3.example.com"
    summary = str(settings.safe_summary())
    assert "plummy-docs" in summary
    assert SECRET not in summary
    assert SECRET not in repr(settings)


@pytest.mark.parametrize(
    "missing", ["s3_endpoint_url", "s3_bucket", "s3_access_key_id", "s3_secret_access_key"]
)
def test_s3_backend_needs_every_value(missing: str) -> None:
    values = {**S3_SETTINGS, missing: None}
    with pytest.raises(ConfigurationError, match="STORAGE_BACKEND=s3 requires"):
        make_settings(**values)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("s3_endpoint_url", "ftp://s3.example.com"),
        ("s3_endpoint_url", "https://s3.example.com/bucket"),
        ("s3_endpoint_url", "https://user:pass@s3.example.com"),
        ("s3_endpoint_url", "https://s3.example.com?x=1"),
        ("s3_bucket", "UPPER"),
        ("s3_bucket", "ab"),
        ("s3_bucket", "a..b"),
        ("s3_bucket", "-bucket"),
        ("s3_region", "us east"),
        ("s3_prefix", "no-trailing-slash"),
        ("s3_prefix", "../"),
        ("s3_prefix", "/leading/"),
        ("s3_prefix", "UPPER/"),
    ],
)
def test_invalid_s3_values_are_refused(field: str, value: str) -> None:
    with pytest.raises(ConfigurationError):
        make_settings(**{**S3_SETTINGS, field: value})


def test_secret_key_can_come_from_a_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "s3-secret"
    path.write_text(SECRET + "\n")
    monkeypatch.setenv("S3_SECRET_ACCESS_KEY_FILE", str(path))
    values = {key: value for key, value in S3_SETTINGS.items() if key != "s3_secret_access_key"}
    settings = make_settings(**values)
    assert settings.s3_secret_access_key is not None
    assert settings.s3_secret_access_key.get_secret_value() == SECRET


def test_plain_http_endpoint_needs_an_allowlist_entry(tmp_path: Path) -> None:
    settings = make_settings(**{**S3_SETTINGS, "s3_endpoint_url": "http://s3.internal:7070"})
    with pytest.raises(ConfigurationError, match=r'purposes = \["storage"\]'):
        check_storage_configuration(settings)
    config = tmp_path / "plummy.toml"
    config.write_text(
        '[[network.allowlist]]\nname = "s3"\nurl = "http://s3.internal:7070"\npurposes = ["storage"]\n'
        'allowed_cidrs = ["10.20.30.40/32"]\n'
    )
    allowed = make_settings(
        **{**S3_SETTINGS, "s3_endpoint_url": "http://s3.internal:7070"}, config_file=config
    )
    check_storage_configuration(allowed)


def test_an_entry_for_another_purpose_does_not_open_storage(tmp_path: Path) -> None:
    config = tmp_path / "plummy.toml"
    config.write_text(
        '[[network.allowlist]]\nname = "llm"\nurl = "http://s3.internal:7070"\npurposes = ["llm"]\n'
        'allowed_cidrs = ["10.20.30.40/32"]\n'
    )
    settings = make_settings(
        **{**S3_SETTINGS, "s3_endpoint_url": "http://s3.internal:7070"}, config_file=config
    )
    with pytest.raises(ConfigurationError):
        check_storage_configuration(settings)


async def test_create_storage_chooses_the_backend() -> None:
    s3 = create_storage(make_settings(**S3_SETTINGS))
    assert isinstance(s3, S3Storage)
    await s3.aclose()
    assert not isinstance(create_storage(make_settings()), S3Storage)


# --- listing ------------------------------------------------------------------------------------------------


NS = 'xmlns="http://s3.amazonaws.com/doc/2006-03-01/"'
OWN = "p/quarantine/ab/0123456789abcdef0123456789abcdab"


def listing(*items: tuple[str, str], token: str | None = None) -> bytes:
    contents = "".join(
        f"<Contents><Key>{key}</Key><LastModified>{when}</LastModified></Contents>" for key, when in items
    )
    truncated = f"<IsTruncated>true</IsTruncated><NextContinuationToken>{token}</NextContinuationToken>"
    tail = truncated if token else "<IsTruncated>false</IsTruncated>"
    return f"<ListBucketResult {NS}>{contents}{tail}</ListBucketResult>".encode()


def test_listing_keeps_only_old_objects_of_this_layout() -> None:
    body = listing(
        (OWN, "2026-01-01T00:00:00.000Z"),
        ("p/quarantine/ab/0123456789abcdef0123456789abcdac", "2026-12-01T00:00:00Z"),  # fresh
        ("p/quarantine/foreign-file.txt", "2020-01-01T00:00:00Z"),  # not ours: never touched
        ("p/quarantine/ab/../../documents/x", "2020-01-01T00:00:00Z"),
        ("p/quarantine/ab/0123456789abcdef0123456789abcdad", "not a date"),
    )
    expired, token = _parse_listing(body, _own_key("p/quarantine/"), datetime(2026, 6, 1, tzinfo=UTC))
    assert expired == [OWN]
    assert token is None


def test_listing_returns_the_continuation_token() -> None:
    body = listing((OWN, "2026-01-01T00:00:00Z"), token="next/+=")
    assert _parse_listing(body, _own_key("p/quarantine/"), datetime(2026, 6, 1, tzinfo=UTC))[1] == "next/+="


def test_broken_listing_is_unavailable() -> None:
    with pytest.raises(StorageUnavailableError):
        _parse_listing(b"<not xml", _own_key(""), datetime.now(UTC))


# --- the adapter against a scripted server ------------------------------------------------------------------


def storage_for(port: int, tmp_path: Path) -> S3Storage:
    config = (
        f'[[network.allowlist]]\nname = "fake-s3"\nurl = "http://127.0.0.1:{port}"\npurposes = ["storage"]\n'
        'allowed_cidrs = ["127.0.0.1/32"]\n'
    )
    http = SafeHttpClient(build_egress_policy(parse_instance_config(config.encode())))
    return S3Storage(
        http,
        endpoint_url=f"http://127.0.0.1:{port}",
        bucket="docs",
        region="us-east-1",
        credentials=Credentials("app", SECRET),
        prefix="p/",
        temp_dir=tmp_path,
    )


KEY = StorageKey("0123456789abcdef0123456789abcdab")


async def test_put_sends_one_signed_conditional_request(tmp_path: Path) -> None:
    received: list[ReceivedRequest] = []

    def handler(request: ReceivedRequest) -> Reply:
        received.append(request)
        return Reply(200)

    async with serve(handler) as server:
        storage = storage_for(server.port, tmp_path)
        stored = await storage.put(StorageArea.QUARANTINE, KEY, stream(b"hello ", b"world"))
        await storage.aclose()
    (request,) = received
    assert request.method == "PUT"
    assert request.target == f"/docs/p/quarantine/ab/{KEY.value}"
    assert request.body == b"hello world"
    headers = {name.lower(): value for name, value in request.headers.items()}
    assert headers["if-none-match"] == "*"
    assert headers["x-amz-content-sha256"] == stored.sha256.hex()
    assert headers["authorization"].startswith("AWS4-HMAC-SHA256 Credential=app/")
    assert SECRET not in str(request.headers)
    assert stored.size_bytes == 11
    assert list((tmp_path / "plummy-storage").iterdir()) == []  # the spool file is gone


@pytest.mark.parametrize(
    ("status", "error"),
    [(412, StorageObjectExistsError), (403, StorageUnavailableError), (500, StorageUnavailableError)],
)
async def test_put_answers_are_typed(tmp_path: Path, status: int, error: type[Exception]) -> None:
    async with serve(lambda _request: Reply(status, body=b"<Error><Code>X</Code></Error>")) as server:
        storage = storage_for(server.port, tmp_path)
        with pytest.raises(error):
            await storage.put(StorageArea.QUARANTINE, KEY, stream(b"x"))
        await storage.aclose()


async def test_errors_do_not_leak_keys_or_secrets(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    async with serve(lambda _request: Reply(500, body=b"internal details")) as server:
        storage = storage_for(server.port, tmp_path)
        with pytest.raises(StorageUnavailableError) as info:
            await storage.exists(StorageArea.DOCUMENTS, KEY)
        await storage.aclose()
    ours = "\n".join(record.getMessage() for record in caplog.records if record.name.startswith("plummy"))
    assert "storage head answered 500" in ours
    for text in (str(info.value), ours):
        assert KEY.value not in text
        assert SECRET not in text
        assert "internal details" not in text


async def test_copy_with_an_error_document_is_not_a_success(tmp_path: Path) -> None:
    def handler(request: ReceivedRequest) -> Reply:
        if request.method == "HEAD":
            return Reply(404)
        return Reply(200, body=b"<Error><Code>InternalError</Code></Error>")

    async with serve(handler) as server:
        storage = storage_for(server.port, tmp_path)
        with pytest.raises(StorageUnavailableError):
            await storage.move(KEY, StorageArea.QUARANTINE, StorageArea.DOCUMENTS)
        await storage.aclose()


async def test_missing_object_and_failed_download_clean_up(tmp_path: Path) -> None:
    async with serve(lambda _request: Reply(404)) as server:
        storage = storage_for(server.port, tmp_path)
        with pytest.raises(StorageObjectNotFoundError):
            async with storage.local_path(StorageArea.DOCUMENTS, KEY):
                pass
        await storage.aclose()
    assert list((tmp_path / "plummy-storage").iterdir()) == []


async def test_unreachable_or_unlisted_endpoint_is_unavailable(tmp_path: Path) -> None:
    unlisted = S3Storage(
        SafeHttpClient(build_egress_policy(parse_instance_config(b""))),
        endpoint_url="http://127.0.0.1:9",
        bucket="docs",
        region="us-east-1",
        credentials=Credentials("app", SECRET),
        temp_dir=tmp_path,
    )
    with pytest.raises(StorageUnavailableError):
        await unlisted.put(StorageArea.QUARANTINE, KEY, stream(b"x"))
    with pytest.raises(StorageUnavailableError):
        await S3StorageCheck(unlisted).check()
    await unlisted.aclose()


def test_http_client_loggers_do_not_log_request_urls() -> None:
    names = ("", "httpx", "httpcore", "uvicorn", "uvicorn.error", "uvicorn.access", "sqlalchemy")
    saved = {name: (logging.getLogger(name).level, list(logging.getLogger(name).handlers)) for name in names}
    try:
        configure_logging("INFO")
        assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
        assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING
    finally:
        for name, (level, handlers) in saved.items():
            logger = logging.getLogger(name)
            logger.setLevel(level)
            logger.handlers[:] = handlers
