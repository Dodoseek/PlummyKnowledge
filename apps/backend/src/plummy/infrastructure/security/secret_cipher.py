"""AES-256-GCM encryption of stored secrets with a key ring kept outside the database (ADR-016).

Key material never appears in ``repr``, logs or error messages: every error names the problem, not the value.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import re
import secrets
import stat
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final
from uuid import UUID

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from plummy.application.ports import EncryptedSecret, SecretDecryptionError
from plummy.domain.secrets import SecretKind, SecretValue, is_valid_key_id

logger = logging.getLogger(__name__)

ALGORITHM: Final = "AES-256-GCM"
KEY_BYTES: Final = 32
NONCE_BYTES: Final = 12
AAD_PREFIX: Final = "plummy-secret-v1"
MAX_KEYRING_FILE_BYTES: Final = 64 * 1024
# 32 bytes in URL-safe base64: 43 characters, optionally with one "=" of padding.
_KEY_RE: Final = re.compile(r"[A-Za-z0-9_-]{43}=?")


class KeyRingError(Exception):
    """The key ring is missing or invalid. The message never contains key material."""


@dataclass(frozen=True, slots=True)
class KeyRing:
    primary: str
    keys: Mapping[str, bytes] = field(repr=False)

    def __post_init__(self) -> None:
        if not self.keys:
            raise KeyRingError("the key ring has no keys")
        for key_id, key in self.keys.items():
            if not is_valid_key_id(key_id):
                raise KeyRingError("key ids must be 1-32 characters of [A-Za-z0-9._-]")
            if len(key) != KEY_BYTES:
                raise KeyRingError(f"key {key_id!r} must be exactly {KEY_BYTES} bytes")
        if self.primary not in self.keys:
            raise KeyRingError("'primary' must name one of the keys")


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name, value in pairs:
        if name in result:
            raise KeyRingError(f"duplicate entry {name!r}")
        result[name] = value
    return result


def _decode_key(key_id: str, encoded: object) -> bytes:
    if not isinstance(encoded, str) or _KEY_RE.fullmatch(encoded) is None:
        raise KeyRingError(f"key {key_id!r} must be {KEY_BYTES} bytes in URL-safe base64")
    try:
        return base64.urlsafe_b64decode(encoded.rstrip("=") + "=")
    except (binascii.Error, ValueError):  # pragma: no cover - excluded by the pattern above
        raise KeyRingError(f"key {key_id!r} must be {KEY_BYTES} bytes in URL-safe base64") from None


def parse_keyring(data: bytes) -> KeyRing:
    """``{"primary": "<key id>", "keys": {"<key id>": "<base64url, 32 bytes>", ...}}``."""
    try:
        document = json.loads(data.decode("utf-8"), object_pairs_hook=_reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise KeyRingError("the key ring is not valid JSON") from None
    if not isinstance(document, dict) or set(document) != {"primary", "keys"}:
        raise KeyRingError("the key ring must be an object with exactly 'primary' and 'keys'")
    primary, keys = document["primary"], document["keys"]
    if not isinstance(primary, str) or not isinstance(keys, dict):
        raise KeyRingError("'primary' must be a string and 'keys' an object")
    return KeyRing(
        primary=primary, keys={key_id: _decode_key(key_id, value) for key_id, value in keys.items()}
    )


def load_keyring(path: Path) -> KeyRing:
    """Read once at startup. Raises :class:`KeyRingError` (including for an unreadable file)."""
    try:
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise KeyRingError(f"{path} is not a regular file")
        if info.st_size > MAX_KEYRING_FILE_BYTES:
            raise KeyRingError(f"{path} is larger than {MAX_KEYRING_FILE_BYTES} bytes")
        data = path.read_bytes()
    except OSError as exc:
        raise KeyRingError(f"cannot read {path}: {exc.strerror or type(exc).__name__}") from None
    if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        logger.warning(
            "the secrets key ring is accessible to other users; recommended mode is 0400",
            extra={"path": str(path), "mode": oct(stat.S_IMODE(info.st_mode))},
        )
    return parse_keyring(data)


def generate_key() -> str:
    """A new random 256-bit key in URL-safe base64 (never derived from a password)."""
    return base64.urlsafe_b64encode(secrets.token_bytes(KEY_BYTES)).rstrip(b"=").decode("ascii")


def default_key_id(now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).strftime("%Y-%m-%d")


def keyring_document(primary: str, keys: Mapping[str, str]) -> str:
    return json.dumps({"primary": primary, "keys": dict(keys)}, indent=2) + "\n"


def create_keyring_file(path: Path, key_id: str) -> bool:
    """Create a key ring with one new key, mode 0400. ``False`` if the file exists (never overwritten)."""
    if not is_valid_key_id(key_id):
        raise KeyRingError("key ids must be 1-32 characters of [A-Za-z0-9._-]")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    content = keyring_document(key_id, {key_id: generate_key()}).encode("utf-8")
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o400)
    except FileExistsError:
        return False
    try:
        os.write(descriptor, content)
        os.fsync(descriptor)
    except BaseException:
        os.close(descriptor)
        path.unlink(missing_ok=True)
        raise
    os.close(descriptor)
    return True


def associated_data(secret_id: UUID, kind: SecretKind) -> bytes:
    """Binds a ciphertext to its record: it cannot be moved to another row or reused as another kind."""
    return f"{AAD_PREFIX}|{secret_id}|{kind.value}".encode("ascii")


class AesGcmSecretCipher:
    """Implements ``SecretCipher``: the primary key encrypts, any key of the ring decrypts by ``key_id``."""

    def __init__(self, keyring: KeyRing) -> None:
        self._primary = keyring.primary
        self._aeads = {key_id: AESGCM(key) for key_id, key in keyring.keys.items()}

    def __repr__(self) -> str:
        return f"AesGcmSecretCipher(primary={self._primary!r}, keys={len(self._aeads)})"

    @property
    def primary_key_id(self) -> str:
        return self._primary

    @property
    def key_ids(self) -> frozenset[str]:
        return frozenset(self._aeads)

    def encrypt(self, secret_id: UUID, kind: SecretKind, value: SecretValue) -> EncryptedSecret:
        nonce = os.urandom(NONCE_BYTES)
        ciphertext = self._aeads[self._primary].encrypt(
            nonce, value.reveal().encode("utf-8"), associated_data(secret_id, kind)
        )
        return EncryptedSecret(key_id=self._primary, algorithm=ALGORITHM, nonce=nonce, ciphertext=ciphertext)

    def decrypt(self, secret_id: UUID, kind: SecretKind, encrypted: EncryptedSecret) -> SecretValue:
        if encrypted.algorithm != ALGORITHM:
            raise SecretDecryptionError("unsupported algorithm")
        aead = self._aeads.get(encrypted.key_id)
        if aead is None:
            raise SecretDecryptionError("unknown key id")
        if len(encrypted.nonce) != NONCE_BYTES:
            raise SecretDecryptionError("invalid nonce")
        try:
            plaintext = aead.decrypt(encrypted.nonce, encrypted.ciphertext, associated_data(secret_id, kind))
            return SecretValue(plaintext.decode("utf-8"))
        except (InvalidTag, UnicodeDecodeError):
            raise SecretDecryptionError("authentication failed") from None
