"""Stored secrets (ADR-016): kinds, the write-only value wrapper and the display hint."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Final

KEY_ID_PATTERN: Final = re.compile(r"[A-Za-z0-9._-]{1,32}")
HINT_LENGTH: Final = 4
HINT_MIN_SECRET_LENGTH: Final = 16
MAX_SECRET_LENGTH: Final = 8192


class SecretKind(StrEnum):
    AI_API_KEY = "ai_api_key"
    OAUTH_REFRESH_TOKEN = "oauth_refresh_token"  # noqa: S105 - a kind name, not a secret
    OAUTH_CLIENT_SECRET = "oauth_client_secret"  # noqa: S105 - a kind name, not a secret


class SecretValue:
    """A plain-text secret in memory: ``repr``/``str`` never show it; call :meth:`reveal` where it is used."""

    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value = value

    def reveal(self) -> str:
        return self._value

    def __len__(self) -> int:
        return len(self._value)

    def __repr__(self) -> str:
        return "SecretValue('**********')"

    __str__ = __repr__

    def __reduce__(self) -> tuple[object, ...]:
        raise TypeError("secret values cannot be pickled")


def secret_hint(value: SecretValue) -> str | None:
    """The last 4 characters, only for secrets of at least 16 characters (too little to recover the rest)."""
    plain = value.reveal()
    if len(plain) < HINT_MIN_SECRET_LENGTH:
        return None
    return plain[-HINT_LENGTH:]


def is_valid_key_id(key_id: str) -> bool:
    return KEY_ID_PATTERN.fullmatch(key_id) is not None
