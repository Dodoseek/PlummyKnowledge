"""Stores secrets encrypted inside the caller's unit of work and reveals them only at the point of use."""

from __future__ import annotations

import logging
from uuid import UUID

from plummy.application.errors import InvalidInputError, ServiceUnavailableError
from plummy.application.ports import (
    Clock,
    SecretCipher,
    SecretDecryptionError,
    SecretRecord,
    UnitOfWork,
)
from plummy.core.ids import new_id
from plummy.domain.secrets import MAX_SECRET_LENGTH, SecretKind, SecretValue, secret_hint

logger = logging.getLogger(__name__)


class SecretsNotConfiguredError(ServiceUnavailableError):
    """Secret storage is not configured on this server (SECRETS_KEYRING_FILE)."""

    code = "secrets_not_configured"


class SecretUnavailableError(ServiceUnavailableError):
    """A stored secret cannot be decrypted with the configured key ring."""

    code = "secret_unavailable"


def validate_secret(field: str, value: str) -> SecretValue:
    """Business rules for a secret typed by an administrator. Error messages never contain the value."""
    if not value or not value.strip():
        raise InvalidInputError(field, "secret_empty", "The secret must not be empty.")
    if len(value) > MAX_SECRET_LENGTH:
        raise InvalidInputError(
            field, "secret_too_long", f"The secret must be at most {MAX_SECRET_LENGTH} characters."
        )
    if value != value.strip() or any(ord(char) < 0x20 or ord(char) == 0x7F for char in value):
        raise InvalidInputError(
            field,
            "secret_invalid",
            "The secret must not contain whitespace at the ends or control characters.",
        )
    return SecretValue(value)


class SecretVault:
    """Every write happens in the caller's transaction, together with the record that owns the secret."""

    def __init__(self, cipher: SecretCipher | None, clock: Clock) -> None:
        self._cipher_or_none = cipher
        self._clock = clock

    @property
    def configured(self) -> bool:
        return self._cipher_or_none is not None

    def _cipher(self) -> SecretCipher:
        if self._cipher_or_none is None:
            raise SecretsNotConfiguredError
        return self._cipher_or_none

    async def store(self, uow: UnitOfWork, kind: SecretKind, value: SecretValue) -> SecretRecord:
        secret_id = new_id()
        record = SecretRecord(
            id=secret_id,
            kind=kind,
            encrypted=self._cipher().encrypt(secret_id, kind, value),
            hint=secret_hint(value),
        )
        await uow.secrets.add(record, at=self._clock.now())
        return record

    async def replace(self, uow: UnitOfWork, secret_id: UUID, value: SecretValue) -> SecretRecord:
        current = await uow.secrets.get_for_update(secret_id)
        if current is None:
            raise SecretUnavailableError
        encrypted = self._cipher().encrypt(secret_id, current.kind, value)
        hint = secret_hint(value)
        await uow.secrets.replace(secret_id, encrypted, hint=hint, at=self._clock.now())
        return SecretRecord(id=secret_id, kind=current.kind, encrypted=encrypted, hint=hint)

    async def reveal(self, uow: UnitOfWork, secret_id: UUID) -> SecretValue:
        record = await uow.secrets.get(secret_id)
        if record is None:
            raise SecretUnavailableError
        try:
            return self._cipher().decrypt(record.id, record.kind, record.encrypted)
        except SecretDecryptionError as exc:
            # Only identifiers: never the value, the ciphertext or key material.
            logger.error(
                "secret decryption failed",
                extra={
                    "secret_id": str(record.id),
                    "key_id": record.encrypted.key_id,
                    "error_type": type(exc).__name__,
                },
            )
            raise SecretUnavailableError from None
