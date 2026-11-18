"""Server-side secret maintenance (``python -m plummy.cli secrets ...``, ADR-016 §3)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from uuid import UUID

from plummy.application.ports import Clock, SecretCipher, SecretDecryptionError, UnitOfWorkFactory
from plummy.domain.audit import ActorType, AuditAction, AuditEntry, AuditOutcome

logger = logging.getLogger(__name__)

ROTATION_BATCH_SIZE = 100


class _Outcome(Enum):
    ROTATED = "rotated"
    SKIPPED = "skipped"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class SecretsStatus:
    configured: bool
    primary_key_id: str | None
    key_ids: frozenset[str]
    records_by_key: dict[str, int]

    @property
    def total(self) -> int:
        return sum(self.records_by_key.values())

    @property
    def on_old_keys(self) -> int:
        """Records still waiting for ``secrets rotate``."""
        return sum(count for key_id, count in self.records_by_key.items() if key_id != self.primary_key_id)

    @property
    def unknown_key_ids(self) -> frozenset[str]:
        """Key ids used in the database but missing from the key ring: those secrets cannot be decrypted."""
        return frozenset(self.records_by_key) - self.key_ids


class GetSecretsStatus:
    def __init__(self, uow: UnitOfWorkFactory, cipher: SecretCipher | None) -> None:
        self._uow = uow
        self._cipher = cipher

    async def execute(self) -> SecretsStatus:
        async with self._uow() as uow:
            counts = await uow.secrets.count_by_key_id()
        return SecretsStatus(
            configured=self._cipher is not None,
            primary_key_id=self._cipher.primary_key_id if self._cipher else None,
            key_ids=self._cipher.key_ids if self._cipher else frozenset(),
            records_by_key=counts,
        )


@dataclass(frozen=True, slots=True)
class RotationReport:
    primary_key_id: str
    rotated: int
    failed: int


class RotateSecrets:
    """Re-encrypts every record that is not on the primary key.

    Each record is handled in its own transaction under a row lock, so the command can be interrupted and run
    again at any time: records already on the primary key are skipped.
    """

    def __init__(
        self,
        uow: UnitOfWorkFactory,
        cipher: SecretCipher,
        clock: Clock,
        *,
        batch_size: int = ROTATION_BATCH_SIZE,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        self._uow = uow
        self._cipher = cipher
        self._clock = clock
        self._batch_size = batch_size

    async def execute(self) -> RotationReport:
        primary = self._cipher.primary_key_id
        rotated = failed = 0
        after: UUID | None = None
        while True:
            async with self._uow() as uow:
                batch = await uow.secrets.ids_not_on_key(primary, after=after, limit=self._batch_size)
            for secret_id in batch:
                outcome = await self._rotate_one(secret_id, primary)
                rotated += outcome is _Outcome.ROTATED
                failed += outcome is _Outcome.FAILED
            if len(batch) < self._batch_size:
                break
            after = batch[-1]
        async with self._uow() as uow:
            await uow.audit.record(
                AuditEntry(
                    action=AuditAction.SECRETS_ROTATED,
                    outcome=AuditOutcome.SUCCESS if failed == 0 else AuditOutcome.FAILURE,
                    actor_type=ActorType.SYSTEM,
                    details={"primary_key_id": primary, "rotated": rotated, "failed": failed},
                )
            )
            await uow.commit()
        return RotationReport(primary_key_id=primary, rotated=rotated, failed=failed)

    async def _rotate_one(self, secret_id: UUID, primary: str) -> _Outcome:
        async with self._uow() as uow:
            record = await uow.secrets.get_for_update(secret_id)
            if record is None or record.encrypted.key_id == primary:
                return _Outcome.SKIPPED  # deleted or rotated by a concurrent run meanwhile
            try:
                value = self._cipher.decrypt(record.id, record.kind, record.encrypted)
            except SecretDecryptionError as exc:
                logger.error(
                    "secret rotation failed",
                    extra={
                        "secret_id": str(record.id),
                        "key_id": record.encrypted.key_id,
                        "error_type": type(exc).__name__,
                    },
                )
                return _Outcome.FAILED
            encrypted = self._cipher.encrypt(record.id, record.kind, value)
            await uow.secrets.reencrypt(record.id, encrypted, at=self._clock.now())
            await uow.commit()
            return _Outcome.ROTATED
