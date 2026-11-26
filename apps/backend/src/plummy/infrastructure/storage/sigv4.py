"""AWS Signature Version 4 for S3 requests (ADR-022 §1). Standard library only.

Signs the method, path, query, the listed headers and the payload hash; the result is the ``Authorization``
header plus ``x-amz-date`` and ``x-amz-content-sha256``. The secret key never leaves this module except as
an HMAC key, and nothing here logs.
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final
from urllib.parse import quote

ALGORITHM: Final = "AWS4-HMAC-SHA256"
EMPTY_SHA256: Final = hashlib.sha256(b"").hexdigest()
_UNRESERVED: Final = "-_.~"


@dataclass(frozen=True, slots=True)
class Credentials:
    access_key_id: str
    secret_access_key: str

    def __repr__(self) -> str:  # never show the secret, not even in tracebacks
        return f"Credentials(access_key_id={self.access_key_id!r})"


def uri_encode(value: str, *, keep_slash: bool) -> str:
    """RFC 3986 encoding as SigV4 requires: everything but ``A-Za-z0-9-_.~`` (and ``/`` in a path)."""
    return quote(value, safe=_UNRESERVED + ("/" if keep_slash else ""))


def canonical_query(params: Mapping[str, str]) -> str:
    pairs = sorted(
        (uri_encode(k, keep_slash=False), uri_encode(v, keep_slash=False)) for k, v in params.items()
    )
    return "&".join(f"{key}={value}" for key, value in pairs)


def _hmac(key: bytes, message: str) -> bytes:
    return hmac.new(key, message.encode(), hashlib.sha256).digest()


def signing_key(secret: str, date: str, region: str, service: str) -> bytes:
    key = _hmac(f"AWS4{secret}".encode(), date)
    key = _hmac(key, region)
    key = _hmac(key, service)
    return _hmac(key, "aws4_request")


def sign(
    *,
    method: str,
    host: str,
    path: str,
    params: Mapping[str, str],
    headers: Mapping[str, str],
    payload_sha256: str,
    credentials: Credentials,
    region: str,
    now: datetime,
    service: str = "s3",
) -> dict[str, str]:
    """Headers to add to the request: ``Authorization``, ``x-amz-date`` and ``x-amz-content-sha256``.

    ``path`` is the decoded absolute path (``/bucket/key``); it is encoded here exactly once. ``host`` is
    the ``Host`` header value (with the port when it is not the scheme's default). Every header in
    ``headers`` is signed."""
    moment = now.astimezone(UTC)
    amz_date = moment.strftime("%Y%m%dT%H%M%SZ")
    date = amz_date[:8]
    signed = {name.lower(): " ".join(str(value).split()) for name, value in headers.items()}
    signed["host"] = host
    signed["x-amz-date"] = amz_date
    signed["x-amz-content-sha256"] = payload_sha256
    names = sorted(signed)
    canonical_headers = "".join(f"{name}:{signed[name]}\n" for name in names)
    signed_headers = ";".join(names)
    canonical_request = "\n".join(
        [
            method.upper(),
            uri_encode(path, keep_slash=True),
            canonical_query(params),
            canonical_headers,
            signed_headers,
            payload_sha256,
        ]
    )
    scope = f"{date}/{region}/{service}/aws4_request"
    string_to_sign = "\n".join(
        [ALGORITHM, amz_date, scope, hashlib.sha256(canonical_request.encode()).hexdigest()]
    )
    key = signing_key(credentials.secret_access_key, date, region, service)
    signature = hmac.new(key, string_to_sign.encode(), hashlib.sha256).hexdigest()
    return {
        "Authorization": (
            f"{ALGORITHM} Credential={credentials.access_key_id}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        ),
        "x-amz-date": amz_date,
        "x-amz-content-sha256": payload_sha256,
    }
