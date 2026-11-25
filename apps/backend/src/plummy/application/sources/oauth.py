"""PKCE and ``state`` values of the OAuth start (RFC 7636, ADR-020 §1). Pure functions over the CSPRNG."""

from __future__ import annotations

import base64
import hashlib
import secrets
from typing import Final

STATE_BYTES: Final = 32
VERIFIER_BYTES: Final = 64  # 86 base64url characters, inside RFC 7636's 43-128
MAX_STATE_LENGTH: Final = 128
STATE_TTL_SECONDS: Final = 600


def new_state() -> str:
    return secrets.token_urlsafe(STATE_BYTES)


def new_code_verifier() -> str:
    return secrets.token_urlsafe(VERIFIER_BYTES)


def code_challenge(verifier: str) -> str:
    """``S256``: base64url(SHA-256(verifier)) without padding."""
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def state_key(state: str) -> str:
    """The store key: the hash, so a leaked store dump does not hold usable ``state`` values."""
    return hashlib.sha256(state.encode("utf-8")).hexdigest()


def plausible_state(state: str | None) -> bool:
    return (
        state is not None
        and 16 <= len(state) <= MAX_STATE_LENGTH
        and all(char.isascii() and (char.isalnum() or char in "-_") for char in state)
    )
