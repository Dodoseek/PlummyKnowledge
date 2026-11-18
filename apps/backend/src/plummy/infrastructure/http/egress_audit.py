"""Records refused outbound connections as ``network.egress.denied`` (ADR-007 §4.9)."""

from __future__ import annotations

from plummy.application.ports import UnitOfWorkFactory
from plummy.domain.audit import ActorType, AuditAction, AuditEntry, AuditOutcome
from plummy.infrastructure.http.safe_client import EgressDenial


class EgressDenialAuditor:
    """Own transaction: the refusal is recorded even when the caller's transaction rolls back."""

    def __init__(self, uow: UnitOfWorkFactory, actor_type: ActorType = ActorType.SYSTEM) -> None:
        self._uow = uow
        self._actor_type = actor_type

    async def __call__(self, denial: EgressDenial) -> None:
        details: dict[str, str | int | bool] = {
            "purpose": denial.purpose.value,
            "scheme": denial.scheme,
            "host": denial.host,
            "port": denial.port,
            "reason": denial.reason.value,
        }
        if denial.provider_type is not None:
            details["provider_type"] = denial.provider_type
        if denial.rule is not None:
            details["rule"] = denial.rule
        async with self._uow() as uow:
            await uow.audit.record(
                AuditEntry(
                    action=AuditAction.NETWORK_EGRESS_DENIED,
                    outcome=AuditOutcome.DENIED,
                    actor_type=self._actor_type,
                    details=details,
                )
            )
            await uow.commit()
