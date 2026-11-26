"""Retention of the audit log (ADR-024 §6, DATABASE_SCHEMA.md §8).

``python -m plummy.cli maintenance prune-audit``, started by the host's cron or systemd timer in the
``migrate`` container: the services' runtime role cannot delete audit records, only the schema owner can.
Records are deleted oldest first in bounded transactions; the run itself is then recorded with the count and
the cutoff.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Final

from plummy.application.ports import Clock, UnitOfWorkFactory
from plummy.domain.audit import ActorType, AuditAction, AuditEntry, AuditOutcome

AUDIT_PRUNE_BATCH: Final = 5000


@dataclass(frozen=True, slots=True)
class AuditPruneReport:
    cutoff: datetime
    records: int  # deleted, or with ``dry_run`` the number that would be
    dry_run: bool


class PruneAuditLog:
    def __init__(self, uow: UnitOfWorkFactory, clock: Clock, *, batch: int = AUDIT_PRUNE_BATCH) -> None:
        if batch < 1:
            raise ValueError("batch must be positive")
        self._uow, self._clock, self._batch = uow, clock, batch

    async def execute(self, retention: timedelta, *, dry_run: bool = False) -> AuditPruneReport:
        if retention <= timedelta(0):
            raise ValueError("retention must be positive")
        cutoff = self._clock.now() - retention
        if dry_run:
            async with self._uow() as uow:
                return AuditPruneReport(cutoff, await uow.audit.count_older_than(cutoff), dry_run=True)
        deleted = 0
        while True:
            async with self._uow() as uow:
                removed = await uow.audit.delete_older_than(cutoff, limit=self._batch)
                await uow.commit()
            deleted += removed
            if removed < self._batch:
                break
        async with self._uow() as uow:
            await uow.audit.record(
                AuditEntry(
                    action=AuditAction.MAINTENANCE_AUDIT_PRUNED,
                    outcome=AuditOutcome.SUCCESS,
                    actor_type=ActorType.SYSTEM,
                    details={"cutoff": cutoff.isoformat(), "deleted": deleted},
                )
            )
            await uow.commit()
        return AuditPruneReport(cutoff, deleted, dry_run=False)
