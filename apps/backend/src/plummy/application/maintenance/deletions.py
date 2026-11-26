"""Deletions after a backup, carried over to a restored instance (ADR-023 §6, SECURITY_MODEL.md §16.2).

A backup holds the data as of the dump; whatever was deleted later is back after a restore. Before the restore
the administrator exports, from the source database, the identifiers of the deletions made since the dump
(``maintenance export-deletions``); after the restore the deletion code of the API's use cases deletes them
again (``maintenance reapply-deletions``), as a system action with its own audit record. The export carries
identifiers, types and times only: no names, emails or content.

Some changes after the dump are security-relevant but are not deletions (a disabled user, a revoked role, a
changed password, a revoked share). They are not reapplied automatically; the export counts them so the
administrator can review them in the source audit log.
"""

from __future__ import annotations

import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Final
from uuid import UUID

from plummy.application.documents.use_cases import delete_document_in
from plummy.application.errors import LastAdministratorError
from plummy.application.knowledge.use_cases import delete_knowledge_base_in, finish_knowledge_base_deletion
from plummy.application.ports import Clock, JobQueue, JobQueueUnavailableError, UnitOfWorkFactory
from plummy.application.secret_storage.vault import SecretVault
from plummy.application.sources.ports import OAuthClients
from plummy.application.sources.use_cases import (
    delete_source_in,
    disconnect_source_in,
    finish_source_deletion,
    revoke_token,
)
from plummy.application.users.use_cases import delete_user_in
from plummy.domain.audit import ActorType, AuditAction, AuditEntry, AuditOutcome
from plummy.domain.knowledge import DataSourceStatus

logger = logging.getLogger(__name__)

FORMAT: Final = "plummy-deletions/1"
MAX_DELETIONS: Final = 1_000_000

# Reapplied, in the order they happened. Each maps to the target type its audit record names.
REAPPLIED: Final[dict[str, str]] = {
    AuditAction.DOCUMENT_DELETED.value: "document",
    AuditAction.KB_DELETED.value: "knowledge_base",
    AuditAction.DATA_SOURCE_DISCONNECTED.value: "data_source",
    AuditAction.DATA_SOURCE_DELETED.value: "data_source",
    AuditAction.CONVERSATION_DELETED.value: "conversation",
    AuditAction.USER_DELETED.value: "user",
}

# Counted for the administrator's review, never reapplied.
REVIEWED: Final[tuple[str, ...]] = tuple(
    action.value
    for action in (
        AuditAction.USER_DISABLED,
        AuditAction.USER_UPDATED,
        AuditAction.ROLE_REVOKED,
        AuditAction.AUTH_PASSWORD_CHANGED,
        AuditAction.ADMIN_PASSWORD_RESET_VIA_CLI,
        AuditAction.AUTH_SESSION_REVOKED,
        AuditAction.AUTH_SESSION_REVOKED_BY_ADMIN,
        AuditAction.KB_ACCESS_REVOKED,
        AuditAction.GROUP_MEMBERS_CHANGED,
        AuditAction.GROUP_DELETED,
        AuditAction.ASSISTANT_DELETED,
        AuditAction.PROVIDER_DELETED,
        AuditAction.PROVIDER_SECRET_ROTATED,
        AuditAction.MODEL_DELETED,
        AuditAction.SECRETS_ROTATED,
    )
)


class DeletionsFileError(Exception):
    """The deletions file is not a valid export. The message never contains file content."""


@dataclass(frozen=True, slots=True)
class Deletion:
    action: str
    target_id: UUID
    at: datetime


@dataclass(frozen=True, slots=True)
class DeletionExport:
    since: datetime
    exported_at: datetime
    deletions: list[Deletion]
    not_reapplied: dict[str, int]

    def to_json(self) -> str:
        return json.dumps(
            {
                "format": FORMAT,
                "since": self.since.isoformat(),
                "exported_at": self.exported_at.isoformat(),
                "deletions": [
                    {
                        "action": item.action,
                        "target_type": REAPPLIED[item.action],
                        "target_id": str(item.target_id),
                        "at": item.at.isoformat(),
                    }
                    for item in self.deletions
                ],
                "not_reapplied": self.not_reapplied,
            },
            indent=2,
        )

    @classmethod
    def from_json(cls, text: str) -> DeletionExport:
        try:
            data = json.loads(text)
        except ValueError:
            raise DeletionsFileError("not JSON") from None
        if not isinstance(data, dict) or data.get("format") != FORMAT:
            raise DeletionsFileError(f"not a {FORMAT} export")
        items = data.get("deletions")
        counts = data.get("not_reapplied", {})
        if not isinstance(items, list) or len(items) > MAX_DELETIONS or not isinstance(counts, dict):
            raise DeletionsFileError("malformed export")
        try:
            deletions = [_deletion(item) for item in items]
            return cls(
                since=_aware(data["since"]),
                exported_at=_aware(data["exported_at"]),
                deletions=deletions,
                not_reapplied={str(name): int(count) for name, count in counts.items()},
            )
        except (KeyError, TypeError, ValueError):
            raise DeletionsFileError("malformed export") from None


def _aware(value: Any) -> datetime:
    if not isinstance(value, str):
        raise TypeError
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError
    return parsed


def _deletion(item: Any) -> Deletion:
    if not isinstance(item, dict):
        raise TypeError
    action = item["action"]
    if action not in REAPPLIED or item.get("target_type") != REAPPLIED[action]:
        raise ValueError
    return Deletion(action=action, target_id=UUID(str(item["target_id"])), at=_aware(item["at"]))


class ExportDeletions:
    """Run on the source database: the deletions since ``since`` (the dump's start time from the manifest)."""

    def __init__(self, uow: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def execute(self, since: datetime) -> DeletionExport:
        if since.tzinfo is None:
            raise ValueError("since must be timezone-aware")
        async with self._uow() as uow:
            records = await uow.audit.successful_since(since, REAPPLIED.keys())
            reviewed = await uow.audit.count_successful_since(since, REVIEWED)
            export = DeletionExport(
                since=since.astimezone(UTC),
                exported_at=self._clock.now(),
                deletions=[
                    Deletion(record.action, record.target_id, record.created_at)
                    for record in records
                    if record.target_id is not None and record.target_type == REAPPLIED[record.action]
                ],
                not_reapplied=dict(sorted(reviewed.items())),
            )
            await uow.audit.record(
                AuditEntry(
                    action=AuditAction.MAINTENANCE_DELETIONS_EXPORTED,
                    outcome=AuditOutcome.SUCCESS,
                    actor_type=ActorType.SYSTEM,
                    details={"since": export.since.isoformat(), "deletions": len(export.deletions)},
                )
            )
            await uow.commit()
        return export


@dataclass(slots=True)
class ReapplyReport:
    applied: Counter[str] = field(default_factory=Counter)
    skipped: Counter[str] = field(default_factory=Counter)  # absent or already deleted: nothing to do
    refused: Counter[str] = field(default_factory=Counter)  # e.g. the last active administrator


class ReapplyDeletions:
    """Run on the restored instance after ``migrate``. Idempotent: what is already deleted is skipped."""

    def __init__(
        self,
        uow: UnitOfWorkFactory,
        queue: JobQueue,
        clock: Clock,
        vault: SecretVault,
        clients: OAuthClients,
    ) -> None:
        self._uow, self._queue, self._clock = uow, queue, clock
        self._vault, self._clients = vault, clients

    async def execute(self, export: DeletionExport) -> ReapplyReport:
        report = ReapplyReport()
        for item in export.deletions:
            outcome = await self._apply(item)
            getattr(report, outcome)[item.action] += 1
        return report

    async def _apply(self, item: Deletion) -> str:
        handlers = {
            AuditAction.DOCUMENT_DELETED.value: self._document,
            AuditAction.KB_DELETED.value: self._knowledge_base,
            AuditAction.DATA_SOURCE_DISCONNECTED.value: self._disconnect,
            AuditAction.DATA_SOURCE_DELETED.value: self._data_source,
            AuditAction.CONVERSATION_DELETED.value: self._conversation,
            AuditAction.USER_DELETED.value: self._user,
        }
        return await handlers[item.action](item)

    def _audit(self, item: Deletion) -> AuditEntry:
        return AuditEntry(
            action=AuditAction.MAINTENANCE_DELETION_REAPPLIED,
            outcome=AuditOutcome.SUCCESS,
            actor_type=ActorType.SYSTEM,
            target_type=REAPPLIED[item.action],
            target_id=item.target_id,
            details={"deletion": item.action, "deleted_at": item.at.isoformat()},
        )

    async def _purge(self, documents: list[UUID]) -> None:
        for document_id in documents:
            try:
                await self._queue.enqueue_purge(document_id)
            except JobQueueUnavailableError:
                logger.warning("purge not published, broker unavailable; the sweep purges it later")
                return

    async def _document(self, item: Deletion) -> str:
        now = self._clock.now()
        async with self._uow() as uow:
            document = await uow.documents.get_for_update(item.target_id)
            if document is None or document.deleted_at is not None:
                return "skipped"
            await delete_document_in(uow, document.id, now=now)
            await uow.audit.record(self._audit(item))
            await uow.commit()
        await self._purge([item.target_id])
        return "applied"

    async def _knowledge_base(self, item: Deletion) -> str:
        now = self._clock.now()
        async with self._uow() as uow:
            kb = await uow.knowledge_bases.get_for_update(item.target_id)
            if kb is None:
                return "skipped"
            deletion = await delete_knowledge_base_in(uow, kb, self._vault, now=now)
            await uow.audit.record(self._audit(item))
            await uow.commit()
        await finish_knowledge_base_deletion(deletion, self._queue, self._clients, kb.id)
        return "applied"

    async def _disconnect(self, item: Deletion) -> str:
        now = self._clock.now()
        async with self._uow() as uow:
            source = await uow.data_sources.get_for_update(item.target_id)
            if (
                source is None
                or source.deleted_at is not None
                or not source.is_external
                or source.status is DataSourceStatus.DISCONNECTED
            ):
                return "skipped"
            disconnection = await disconnect_source_in(uow, self._vault, source, now=now)
            await uow.audit.record(self._audit(item))
            await uow.commit()
        # Most likely revoked already by the original disconnect; a second attempt is harmless.
        await revoke_token(self._clients.get(source.type), disconnection.token, source.id)
        return "applied"

    async def _data_source(self, item: Deletion) -> str:
        now = self._clock.now()
        async with self._uow() as uow:
            source = await uow.data_sources.get_for_update(item.target_id)
            if source is None or source.deleted_at is not None or not source.is_external:
                return "skipped"
            deletion = await delete_source_in(uow, self._vault, source, now=now)
            await uow.audit.record(self._audit(item))
            await uow.commit()
        await finish_source_deletion(deletion, self._queue, self._clients, source)
        return "applied"

    async def _conversation(self, item: Deletion) -> str:
        now = self._clock.now()
        async with self._uow() as uow:
            if not await uow.conversations.mark_deleted(item.target_id, at=now):
                return "skipped"
            await uow.audit.record(self._audit(item))
            await uow.commit()
        return "applied"

    async def _user(self, item: Deletion) -> str:
        now = self._clock.now()
        async with self._uow() as uow:
            await uow.roles.lock_administrators()
            user = await uow.users.get_for_update(item.target_id)
            if user is None or user.deleted_at is not None:
                return "skipped"
            try:
                await delete_user_in(uow, user, by=None, now=now)
            except LastAdministratorError:
                return "refused"
            await uow.audit.record(self._audit(item))
            await uow.commit()
        return "applied"
