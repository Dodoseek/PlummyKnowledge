"""Opaque tokens and the session-bound CSRF token (ADR-013).

Only standard-library primitives: the token values are never stored or logged; the database keeps SHA-256.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets

TOKEN_BYTES = 32  # 256 bits
# ``secrets.token_urlsafe(32)``: 43 characters of the URL-safe base64 alphabet, no padding.
_TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")
_CSRF_MESSAGE = b"plummy-csrf-v1"


def generate_token() -> str:
    """A new random opaque token (session token, bootstrap token)."""
    return secrets.token_urlsafe(TOKEN_BYTES)


def is_well_formed_token(value: str) -> bool:
    """Cheap shape check before hashing: anything else can never match a stored token."""
    return _TOKEN_RE.fullmatch(value) is not None


def hash_token(token: str) -> bytes:
    """SHA-256 of a token. A 256-bit random value needs no salt or slow hash."""
    return hashlib.sha256(token.encode("ascii")).digest()


def derive_csrf_token(session_token: str) -> str:
    """CSRF token bound to the session: HMAC-SHA256 keyed with the session token (ADR-013 §2).

    Deterministic for a session, not stored anywhere, and does not reveal the session token.
    """
    digest = hmac.new(session_token.encode("ascii"), _CSRF_MESSAGE, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def constant_time_equals(left: str | bytes, right: str | bytes) -> bool:
    left_bytes = left.encode("utf-8") if isinstance(left, str) else left
    right_bytes = right.encode("utf-8") if isinstance(right, str) else right
    return hmac.compare_digest(left_bytes, right_bytes)
