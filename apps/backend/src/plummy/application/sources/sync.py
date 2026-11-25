"""Sync of an external source into its knowledge base (ADR-020 §8), run by the ``worker`` (``connectors``).

A job reloads and re-checks everything: the message carries only its ID. Modes:

- **initial / reconciliation** — the change cursor of "now" first, then a full listing of the folder: new and
  changed files are taken in, documents that are no longer listed leave the index (``out_of_scope``). The
  cursor is saved at the end; changes made during the listing are caught up by the next incremental sync.
- **incremental** — the changes since the saved cursor, page by page; the cursor is saved after every fully
  processed page (the checkpoint), so a retried job neither loses nor repeats changes. A rejected cursor or a
  moved or removed folder that holds indexed documents turns the job into a reconciliation.

Rules: a new file becomes a document and a version; another revision a new version (the same content as an
older version indexes that one again); a rename or move only updates the metadata; a trashed or removed file
is deleted (``remote_deleted``), one that left the folder too (``out_of_scope``). Shortcuts are never
followed. A downloaded file goes to quarantine and through the same pipeline as an upload (stages 2-12):
parsing runs in ``worker-ingestion`` without network or secrets.

Every write happens in a transaction that locks the source and then the job and stops if the job was
cancelled (disconnect, deletion, another folder): nothing is written for a source that changed meanwhile.
The access token lives in memory for the job; a ``401`` is answered by one more refresh.
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Final
from uuid import UUID

from plummy.application.ingestion.dispatch import publish
from plummy.application.ports import (
    Clock,
    JobQueue,
    JobQueueUnavailableError,
    UnitOfWork,
    UnitOfWorkFactory,
)
from plummy.application.secret_storage.vault import SecretVault
from plummy.application.sources.connection import (
    AccessTokens,
    ConnectorNotConfiguredError,
    SourceNotConnectedError,
    provider_of,
)
from plummy.application.sources.jobs import add_sync_job, first_mode, publish_sync
from plummy.application.sources.ports import (
    ChangesPage,
    ConnectorAccessTokenRejectedError,
    ConnectorCursorInvalidError,
    ConnectorError,
    ConnectorFileTooLargeError,
    ConnectorNotFoundError,
    ConnectorPermissionDeniedError,
    ConnectorRateLimitedError,
    Connectors,
    DataSourceConnector,
    OAuthClient,
    OAuthClients,
    RemoteChange,
    RemoteItem,
    RemoteKind,
    RemotePage,
)
from plummy.application.storage import (
    StorageArea,
    StorageKey,
    StorageLimitExceededError,
    StorageProvider,
    StoredObject,
)
from plummy.core.ids import new_id
from plummy.domain.documents import (
    DeletionReason,
    Document,
    DocumentStatus,
    DocumentVersion,
    IngestionJob,
    JobKind,
    JobStage,
    JobStatus,
    VersionStatus,
    display_filename,
    ingest_idempotency_key,
)
from plummy.domain.file_types import SNIFF_BYTES, provisional_format, sniff_container
from plummy.domain.knowledge import DataSource, DataSourceStatus, SyncOutcome
from plummy.domain.secrets import SecretValue
from plummy.domain.sources import (
    MAX_FOLDER_DEPTH,
    SyncJob,
    SyncMode,
    SyncStats,
    SyncTrigger,
    next_sync_after,
    reconciliation_due,
)

logger = logging.getLogger(__name__)

INGEST_ATTEMPTS: Final = 5
TRANSIENT: Final = "transient_error"
SUPERSEDED: Final = "superseded_by_sync"
SOURCE_UNAVAILABLE: Final = "source_unavailable"
ATTEMPTS_EXHAUSTED: Final = "attempts_exhausted"
# A running job writes a checkpoint at least after every listing page; one silent this long has no worker.
STALLED_AFTER: Final = timedelta(hours=2)
UNPUBLISHED_AFTER: Final = timedelta(minutes=5)
SCHEDULER_BATCH: Final = 100
ANCESTORS: Final = "ancestors"


class SyncRunOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RETRY = "retry"
    DEAD = "dead"
    SKIPPED = "skipped"


@dataclass(frozen=True, slots=True)
class SyncRunResult:
    outcome: SyncRunOutcome
    retry_after_seconds: float | None = None


class _StoppedError(Exception):
    """The job was finished by someone else (cancelled by a disconnect, a deletion, another folder)."""


@dataclass(slots=True)
class _Run:
    job: SyncJob
    source: DataSource
    root: str
    embedding_profile_id: UUID
    stats: SyncStats
    cursor: str | None
    reconciled: bool = False
    purge: list[UUID] = field(default_factory=list)


def _root_path(root: str) -> str:
    return f"/{root}/"


class _Drive:
    """The connector with the job's access token: refreshed once more when the API refuses it (``401``)."""

    def __init__(
        self, connector: DataSourceConnector, oauth: OAuthClient, tokens: AccessTokens, source_id: UUID
    ) -> None:
        self.connector, self._oauth, self._tokens, self._source_id = connector, oauth, tokens, source_id
        self._token: SecretValue | None = None

    async def _access(self, *, fresh: bool = False) -> SecretValue:
        if self._token is None or fresh:
            self._token = await self._tokens.get(self._oauth, self._source_id)
        return self._token

    async def _call[T](self, request: Callable[[SecretValue], Awaitable[T]]) -> T:
        try:
            return await request(await self._access())
        except ConnectorAccessTokenRejectedError:
            return await request(await self._access(fresh=True))

    async def get_item(self, item_id: str) -> RemoteItem:
        return await self._call(lambda token: self.connector.get_item(token, item_id))

    async def list_children(self, folder_id: str, page_token: str | None) -> RemotePage:
        return await self._call(
            lambda token: self.connector.list_children(token, folder_id, page_token=page_token)
        )

    async def start_cursor(self) -> str:
        return await self._call(self.connector.start_cursor)

    async def get_changes(self, cursor: str) -> ChangesPage:
        return await self._call(lambda token: self.connector.get_changes(token, cursor))

    async def download(self, item: RemoteItem, *, max_bytes: int) -> AsyncIterator[bytes]:
        stream = self.connector.download(await self._access(), item, max_bytes=max_bytes)
        try:
            first = await anext(stream)
        except StopAsyncIteration:
            return
        except ConnectorAccessTokenRejectedError:
            stream = self.connector.download(await self._access(fresh=True), item, max_bytes=max_bytes)
            try:
                first = await anext(stream)
            except StopAsyncIteration:
                return
        yield first
        async for chunk in stream:
            yield chunk


class _Scope:
    """Whether an item lies under the synced folder: its parents are followed up to the folder, at most
    :data:`MAX_FOLDER_DEPTH` levels, with one cache per job. The path is the chain of folder IDs."""

    def __init__(self, drive: _Drive, root: str) -> None:
        self._drive, self._root = drive, root
        self._paths: dict[str, str | None] = {root: _root_path(root)}

    async def path_of(self, item: RemoteItem) -> str | None:
        """The path of the folders that contain ``item``, or ``None`` outside the synced folder."""
        for parent in item.parents:
            path = await self._folder_path(parent, 0)
            if path is not None:
                return path
        return None

    async def _folder_path(self, folder_id: str, depth: int) -> str | None:
        if folder_id in self._paths:
            return self._paths[folder_id]
        if depth >= MAX_FOLDER_DEPTH:
            return None
        try:
            folder = await self._drive.get_item(folder_id)
        except (ConnectorNotFoundError, ConnectorPermissionDeniedError):
            folder = None
        path: str | None = None
        if folder is not None and folder.kind is RemoteKind.FOLDER and not folder.trashed:
            for parent in folder.parents:
                above = await self._folder_path(parent, depth + 1)
                if above is not None:
                    path = f"{above}{folder_id}/"
                    break
        self._paths[folder_id] = path
        return path


class RunSyncJob:
    def __init__(
        self,
        uow: UnitOfWorkFactory,
        storage: StorageProvider,
        clock: Clock,
        vault: SecretVault,
        clients: OAuthClients,
        connectors: Connectors,
        jobs: JobQueue,
        *,
        max_bytes: int,
    ) -> None:
        self._uow, self._storage, self._clock = uow, storage, clock
        self._clients, self._connectors, self._jobs = clients, connectors, jobs
        self._tokens = AccessTokens(uow, clock, vault)
        self._max_bytes = max_bytes

    async def execute(self, job_id: UUID) -> SyncRunResult:
        started = await self._start(job_id)
        if isinstance(started, SyncRunResult):
            return started
        run = started
        try:
            oauth, connector = provider_of(self._clients, self._connectors, run.source)
            await self._sync(run, _Drive(connector, oauth, self._tokens, run.source.id))
        except _StoppedError:
            return await self._after_stop(run)
        except ConnectorNotConfiguredError as exc:
            return await self._fail(run, exc.code)
        except SourceNotConnectedError as exc:
            return await self._fail(run, exc.code)
        except ConnectorError as exc:
            if exc.retryable:
                retry_after = exc.retry_after_seconds if isinstance(exc, ConnectorRateLimitedError) else None
                return await self._transient(run, exc.code, retry_after)
            return await self._fail(run, exc.code)
        except (JobQueueUnavailableError, OSError) as exc:  # storage, database or broker briefly unavailable
            logger.warning(
                "sync failed, will retry",
                extra={"sync_job_id": str(job_id), "error_type": type(exc).__name__},
            )
            return await self._transient(run, TRANSIENT, None)
        return await self._succeed(run)

    # --- lifecycle -----------------------------------------------------------------------------------------

    async def _start(self, job_id: UUID) -> _Run | SyncRunResult:
        now = self._clock.now()
        async with self._uow() as uow:
            unlocked = await uow.sync_jobs.get(job_id)
            if unlocked is None:
                return SyncRunResult(SyncRunOutcome.SKIPPED)
            source = await uow.data_sources.get_for_update(unlocked.data_source_id)
            job = await uow.sync_jobs.get_for_update(job_id)
            if job is None or job.status.finished:
                return SyncRunResult(SyncRunOutcome.SKIPPED)
            kbs = await uow.knowledge_bases.linked_to_source(job.data_source_id) if source else []
            root = source.config.get("folder_id") if source else None
            if (
                source is None
                or source.deleted_at is not None
                or source.status is not DataSourceStatus.ACTIVE
                or not root
                or not kbs
            ):
                job.status, job.error_code = JobStatus.CANCELLED, SOURCE_UNAVAILABLE
                job.finished_at = job.updated_at = now
                await uow.sync_jobs.save(job)
                await uow.commit()
                return SyncRunResult(SyncRunOutcome.CANCELLED)
            job.attempt += 1
            job.status, job.error_code, job.error_message = JobStatus.RUNNING, None, None
            job.started_at = job.started_at or now
            job.updated_at = now
            if job.mode is SyncMode.INCREMENTAL and source.sync_cursor is None:
                job.mode = SyncMode.INITIAL  # the folder changed since the job was queued
            await uow.sync_jobs.save(job)
            await uow.commit()
        return _Run(job, source, root, kbs[0].embedding_profile_id, job.stats, source.sync_cursor)

    async def _locked(self, uow: UnitOfWork, run: _Run) -> tuple[DataSource, SyncJob]:
        """Locks the source and then the job; stops unless both are still as the run expects."""
        source = await uow.data_sources.get_for_update(run.source.id)
        job = await uow.sync_jobs.get_for_update(run.job.id)
        if job is None or job.status is not JobStatus.RUNNING:
            raise _StoppedError()
        if (
            source is None
            or source.deleted_at is not None
            or source.status is not DataSourceStatus.ACTIVE
            or source.config.get("folder_id") != run.root
        ):
            raise _StoppedError()
        return source, job

    async def _checkpoint(self, run: _Run, *, cursor: str | None = None) -> None:
        now = self._clock.now()
        async with self._uow() as uow:
            source, job = await self._locked(uow, run)
            job.stats, job.updated_at = run.stats, now
            if cursor is not None:
                source.sync_cursor = job.cursor_after = cursor
                await uow.data_sources.save(source, at=now)
            await uow.sync_jobs.save(job)
            await uow.commit()
        await self._purge(run)

    async def _succeed(self, run: _Run) -> SyncRunResult:
        now = self._clock.now()
        try:
            async with self._uow() as uow:
                source, job = await self._locked(uow, run)
                job.status, job.stats = JobStatus.SUCCEEDED, run.stats
                job.cursor_after = run.cursor
                job.finished_at = job.updated_at = now
                source.sync_cursor = run.cursor
                source.last_sync_at, source.last_sync_status = now, SyncOutcome.SUCCEEDED
                source.last_error = None
                source.next_sync_at = next_sync_after(now, source.sync_interval_minutes)
                if run.reconciled:
                    source.last_reconciled_at = now
                await uow.sync_jobs.save(job)
                await uow.data_sources.save(source, at=now)
                await uow.commit()
        except _StoppedError:
            return await self._after_stop(run)
        await self._purge(run)
        return SyncRunResult(SyncRunOutcome.SUCCEEDED)

    async def _fail(self, run: _Run, code: str, *, dead: bool = False) -> SyncRunResult:
        """The job ends without retries. The source keeps syncing on schedule unless the grant was revoked
        (then :class:`AccessTokens` already put it into ``error``)."""
        now = self._clock.now()
        async with self._uow() as uow:
            source = await uow.data_sources.get_for_update(run.source.id)
            job = await uow.sync_jobs.get_for_update(run.job.id)
            if job is None or job.status.finished:
                return SyncRunResult(SyncRunOutcome.SKIPPED)
            job.status = JobStatus.DEAD if dead else JobStatus.FAILED
            job.error_code, job.stats = code, run.stats
            job.finished_at = job.updated_at = now
            await uow.sync_jobs.save(job)
            if source is not None and source.deleted_at is None:
                source.last_sync_at, source.last_sync_status = now, SyncOutcome.FAILED
                if source.status is DataSourceStatus.ACTIVE:
                    source.last_error = code
                    source.next_sync_at = next_sync_after(now, source.sync_interval_minutes)
                await uow.data_sources.save(source, at=now)
            await uow.commit()
        await self._purge(run)
        logger.warning("sync failed", extra={"sync_job_id": str(run.job.id), "error_code": code})
        return SyncRunResult(SyncRunOutcome.DEAD if dead else SyncRunOutcome.FAILED)

    async def _transient(self, run: _Run, code: str, retry_after: float | None) -> SyncRunResult:
        if run.job.attempt >= run.job.max_attempts:
            return await self._fail(run, ATTEMPTS_EXHAUSTED, dead=True)
        now = self._clock.now()
        async with self._uow() as uow:
            job = await uow.sync_jobs.get_for_update(run.job.id)
            if job is None or job.status.finished:
                return SyncRunResult(SyncRunOutcome.SKIPPED)
            job.status, job.error_code, job.stats = JobStatus.QUEUED, code, run.stats
            job.updated_at = now
            await uow.sync_jobs.save(job)
            await uow.commit()
        await self._purge(run)
        return SyncRunResult(SyncRunOutcome.RETRY, retry_after)

    async def _after_stop(self, run: _Run) -> SyncRunResult:
        await self._purge(run)
        async with self._uow() as uow:
            job = await uow.sync_jobs.get(run.job.id)
        if job is not None and job.status is JobStatus.RUNNING:
            # The source changed under a running job (paused, another folder): this job ends here.
            now = self._clock.now()
            async with self._uow() as uow:
                job = await uow.sync_jobs.get_for_update(run.job.id)
                if job is not None and job.status is JobStatus.RUNNING:
                    job.status, job.error_code = JobStatus.CANCELLED, SOURCE_UNAVAILABLE
                    job.stats, job.finished_at, job.updated_at = run.stats, now, now
                    await uow.sync_jobs.save(job)
                    await uow.commit()
        return SyncRunResult(SyncRunOutcome.CANCELLED)

    # --- modes ---------------------------------------------------------------------------------------------

    async def _sync(self, run: _Run, drive: _Drive) -> None:
        if run.job.mode is SyncMode.INCREMENTAL and run.cursor is not None:
            try:
                if not await self._incremental(run, drive, run.cursor):
                    return
            except ConnectorCursorInvalidError:
                logger.info("change cursor refused, reconciling", extra={"sync_job_id": str(run.job.id)})
        await self._full(run, drive)

    async def _full(self, run: _Run, drive: _Drive) -> None:
        """Initial sync and reconciliation: the cursor of "now", then the whole folder."""
        cursor = await drive.start_cursor()
        listed = await self._walk(run, drive, run.root, _root_path(run.root))
        async with self._uow() as uow:
            indexed = await uow.documents.live_external_ids(run.source.id)
        for external_id in sorted(set(indexed) - listed):
            await self._remove(run, external_id, DeletionReason.OUT_OF_SCOPE)
        run.cursor, run.reconciled = cursor, True

    async def _walk(self, run: _Run, drive: _Drive, folder_id: str, path: str) -> set[str]:
        """Every non-folder item under the folder, breadth first, at most :data:`MAX_FOLDER_DEPTH` levels;
        returns the IDs of the items seen (a document of an item that could not be taken in stays)."""
        seen: set[str] = set()
        visited = {folder_id}
        pending: deque[tuple[str, str, int]] = deque([(folder_id, path, 0)])
        while pending:
            current, current_path, depth = pending.popleft()
            page_token: str | None = None
            while True:
                page = await drive.list_children(current, page_token)
                for item in page.items:
                    if item.kind is RemoteKind.FOLDER:
                        if depth + 1 < MAX_FOLDER_DEPTH and item.id not in visited:
                            visited.add(item.id)
                            pending.append((item.id, f"{current_path}{item.id}/", depth + 1))
                        continue
                    seen.add(item.id)
                    await self._apply(run, drive, item, current_path)
                await self._checkpoint(run)
                page_token = page.next_page_token
                if page_token is None:
                    break
        return seen

    async def _incremental(self, run: _Run, drive: _Drive, cursor: str) -> bool:
        """The changes since ``cursor``; ``True`` when a reconciliation has to follow."""
        scope = _Scope(drive, run.root)
        reconcile = False
        while True:
            page = await drive.get_changes(cursor)
            for change in page.changes:
                reconcile = await self._change(run, drive, scope, change) or reconcile
            cursor = page.cursor
            await self._checkpoint(run, cursor=cursor)
            run.cursor = cursor
            if page.finished:
                return reconcile

    async def _change(self, run: _Run, drive: _Drive, scope: _Scope, change: RemoteChange) -> bool:
        item = change.item
        if change.removed or item is None:
            await self._remove(run, change.item_id, DeletionReason.REMOTE_DELETED)
            return await self._indexed_under(run, change.item_id)
        if item.kind is RemoteKind.FOLDER:
            return await self._folder_changed(run, drive, scope, item)
        if item.trashed:
            await self._remove(run, item.id, DeletionReason.REMOTE_DELETED)
            return False
        path = await scope.path_of(item)
        if path is None:
            await self._remove(run, item.id, DeletionReason.OUT_OF_SCOPE)
            return False
        await self._apply(run, drive, item, path)
        return False

    async def _folder_changed(self, run: _Run, drive: _Drive, scope: _Scope, folder: RemoteItem) -> bool:
        """A folder changes the scope of everything under it. A folder new to the index (created, or moved in
        with its contents, which Drive does not report one by one) is listed; a folder that holds indexed
        documents and was moved, trashed or left the scope needs a reconciliation; a rename needs nothing."""
        if folder.id == run.root:
            return folder.trashed
        parent_path = None if folder.trashed else await scope.path_of(folder)
        if not await self._indexed_under(run, folder.id):
            if parent_path is not None:
                await self._walk(run, drive, folder.id, f"{parent_path}{folder.id}/")
            return False
        if parent_path is None:
            return True
        async with self._uow() as uow:
            return await uow.documents.has_ancestor(
                run.source.id, folder.id, outside=f"{parent_path}{folder.id}/"
            )

    async def _indexed_under(self, run: _Run, folder_id: str) -> bool:
        async with self._uow() as uow:
            return await uow.documents.has_ancestor(run.source.id, folder_id)

    # --- one item ------------------------------------------------------------------------------------------

    @staticmethod
    def _metadata(item: RemoteItem, path: str) -> dict[str, str]:
        metadata = {ANCESTORS: path, "mime_type": item.mime_type}
        if item.modified_at is not None:
            metadata["modified_at"] = item.modified_at.isoformat()
        return metadata

    async def _apply(self, run: _Run, drive: _Drive, item: RemoteItem, path: str) -> None:
        """A file in scope: taken in when new or changed, its metadata updated when only that changed."""
        if item.kind is not RemoteKind.FILE:
            run.stats.skipped += 1  # shortcuts, forms, sites and other formats without an export
            return
        filename = display_filename(item.filename)
        metadata = self._metadata(item, path)
        async with self._uow() as uow:
            document = await uow.documents.live_by_external_id(run.source.id, item.id)
        if document is not None and item.revision is not None and document.external_etag == item.revision:
            if document.filename != filename or document.source_metadata != metadata:
                await self._update_fields(run, document.id, filename, item.revision, metadata)
                run.stats.renamed += 1
            return
        if item.size is not None and item.size > self._max_bytes:
            run.stats.skipped += 1
            return
        key = StorageKey.new()
        try:
            stored = await self._storage.put(
                StorageArea.QUARANTINE,
                key,
                drive.download(item, max_bytes=self._max_bytes),
                max_bytes=self._max_bytes,
            )
        except (StorageLimitExceededError, ConnectorFileTooLargeError):
            await self._storage.delete(StorageArea.QUARANTINE, key)
            run.stats.skipped += 1
            return
        except (ConnectorNotFoundError, ConnectorPermissionDeniedError):
            await self._storage.delete(StorageArea.QUARANTINE, key)  # gone or hidden since it was listed
            run.stats.failed += 1
            return
        except BaseException:
            await self._storage.delete(StorageArea.QUARANTINE, key)
            raise
        try:
            job = await self._register(run, item, filename, metadata, key, stored)
        except BaseException:
            await self._storage.delete(StorageArea.QUARANTINE, key)
            raise
        if job is not None:
            await publish(self._uow, self._jobs, job)  # unpublished jobs are sent by the maintenance sweep

    async def _head(self, key: StorageKey) -> bytes:
        head = bytearray()
        async for chunk in self._storage.open(StorageArea.QUARANTINE, key):
            head += chunk
            if len(head) >= SNIFF_BYTES:
                break
        return bytes(head[:SNIFF_BYTES])

    async def _register(
        self,
        run: _Run,
        item: RemoteItem,
        filename: str,
        metadata: dict[str, str],
        key: StorageKey,
        stored: StoredObject,
    ) -> IngestionJob | None:
        """The downloaded file as a document version and its ingestion job (one transaction). ``None`` when
        nothing is to be processed; the quarantined file is removed then."""
        kind = (
            sniff_container(await self._head(key), complete=stored.size_bytes <= SNIFF_BYTES)
            if stored.size_bytes
            else None
        )
        if kind is None:  # empty, or a format outside the MVP: decided by the content, as for an upload
            await self._storage.delete(StorageArea.QUARANTINE, key)
            run.stats.skipped += 1
            return None
        file_format = provisional_format(kind, filename)
        now = self._clock.now()
        keep_file = True
        job: IngestionJob | None
        async with self._uow() as uow:
            await self._locked(uow, run)
            document = await uow.documents.live_by_external_id(run.source.id, item.id, lock=True)
            if document is None:
                document = Document(
                    id=new_id(),
                    data_source_id=run.source.id,
                    external_id=item.id,
                    filename=filename,
                    mime_type=file_format.mime_type,
                    processing_status=DocumentStatus.PENDING,
                    current_version_id=None,
                    created_at=now,
                    updated_at=now,
                    source_metadata=metadata,
                    external_etag=item.revision,
                )
                await uow.documents.add(document)
                version = self._new_version(document.id, 1, item, file_format.mime_type, key, stored, now)
                await uow.documents.add_version(version)
                job = self._ingest_job(run, document.id, version, now)
                run.stats.added += 1
            else:
                await uow.documents.update_source_fields(
                    document.id,
                    filename=filename,
                    external_etag=item.revision,
                    source_metadata=metadata,
                    at=now,
                )
                same = await uow.documents.version_with_checksum(document.id, stored.sha256)
                if same is not None:
                    keep_file = False
                    job = await self._same_content(uow, run, document, same, now)
                else:
                    latest = (await uow.documents.latest_versions([document.id]))[document.id]
                    version = self._new_version(
                        document.id, latest.version_number + 1, item, file_format.mime_type, key, stored, now
                    )
                    await uow.ingestion_jobs.cancel_unfinished([document.id], SUPERSEDED, at=now)
                    await uow.documents.add_version(version)
                    job = self._ingest_job(run, document.id, version, now)
                    run.stats.updated += 1
            if job is not None:
                await uow.ingestion_jobs.add(job)
            await uow.commit()
        if not keep_file:
            await self._storage.delete(StorageArea.QUARANTINE, key)
        return job

    async def _same_content(
        self, uow: UnitOfWork, run: _Run, document: Document, version: DocumentVersion, now: datetime
    ) -> IngestionJob | None:
        """A new revision with the content of an existing version: nothing to do when it is the current or the
        newest one; an older one (the content was reverted) is indexed again and becomes current."""
        latest = (await uow.documents.latest_versions([document.id]))[document.id]
        if version.id in (document.current_version_id, latest.id) or version.status is VersionStatus.DELETED:
            return None
        await uow.ingestion_jobs.cancel_unfinished([document.id], SUPERSEDED, at=now)
        job_id = new_id()
        run.stats.updated += 1
        return IngestionJob(
            id=job_id,
            document_id=document.id,
            document_version_id=version.id,
            kind=JobKind.REINDEX,
            status=JobStatus.QUEUED,
            stage=JobStage.CHUNK if version.extracted_storage_key is not None else JobStage.FETCH,
            idempotency_key=f"reindex:{version.id.hex}:{job_id.hex}",
            attempt=0,
            max_attempts=INGEST_ATTEMPTS,
            requested_by=run.job.requested_by,
            queued_at=now,
        )

    @staticmethod
    def _new_version(
        document_id: UUID,
        number: int,
        item: RemoteItem,
        mime_type: str,
        key: StorageKey,
        stored: StoredObject,
        now: datetime,
    ) -> DocumentVersion:
        return DocumentVersion(
            id=new_id(),
            document_id=document_id,
            version_number=number,
            checksum_sha256=stored.sha256,
            mime_type=mime_type,
            size_bytes=stored.size_bytes,
            storage_key=key.value,
            status=VersionStatus.PENDING,
            created_at=now,
            updated_at=now,
            external_version=item.revision,
        )

    def _ingest_job(
        self, run: _Run, document_id: UUID, version: DocumentVersion, now: datetime
    ) -> IngestionJob:
        return IngestionJob(
            id=new_id(),
            document_id=document_id,
            document_version_id=version.id,
            kind=JobKind.INGEST,
            status=JobStatus.QUEUED,
            stage=JobStage.FETCH,
            idempotency_key=ingest_idempotency_key(version.id, run.embedding_profile_id, {}),
            attempt=0,
            max_attempts=INGEST_ATTEMPTS,
            requested_by=run.job.requested_by,
            queued_at=now,
        )

    async def _update_fields(
        self, run: _Run, document_id: UUID, filename: str, revision: str | None, metadata: dict[str, str]
    ) -> None:
        async with self._uow() as uow:
            await self._locked(uow, run)
            await uow.documents.update_source_fields(
                document_id,
                filename=filename,
                external_etag=revision,
                source_metadata=metadata,
                at=self._clock.now(),
            )
            await uow.commit()

    async def _remove(self, run: _Run, external_id: str, reason: DeletionReason) -> None:
        """The document leaves retrieval at once; its data is purged after the commit (or by the sweep)."""
        async with self._uow() as uow:
            if await uow.documents.live_by_external_id(run.source.id, external_id) is None:
                return  # most changes outside the folder concern files that were never indexed
        now = self._clock.now()
        async with self._uow() as uow:
            await self._locked(uow, run)
            document = await uow.documents.live_by_external_id(run.source.id, external_id, lock=True)
            if document is None:
                return
            await uow.ingestion_jobs.cancel_unfinished([document.id], str(reason), at=now)
            await uow.documents.mark_deleted(document.id, reason, at=now)
            await uow.commit()
        run.stats.deleted += 1
        run.purge.append(document.id)

    async def _purge(self, run: _Run) -> None:
        while run.purge:
            document_id = run.purge[0]
            try:
                await self._jobs.enqueue_purge(document_id)
            except JobQueueUnavailableError:
                logger.warning(
                    "purge not published, broker unavailable", extra={"document_id": str(document_id)}
                )
                run.purge.clear()  # the sweep of deleted documents purges them later
                return
            run.purge.pop(0)


class ScheduleSyncs:
    """Celery beat, every minute: an incremental sync (daily a reconciliation) for every active source whose
    ``next_sync_at`` has come; sync jobs the broker never got are sent again and stalled ones requeued."""

    def __init__(self, uow: UnitOfWorkFactory, queue: JobQueue, clock: Clock) -> None:
        self._uow, self._queue, self._clock = uow, queue, clock

    async def execute(self) -> int:
        now = self._clock.now()
        await self._recover(now)
        async with self._uow() as uow:
            due = await uow.data_sources.due_for_sync(now, limit=SCHEDULER_BATCH)
        queued = 0
        for source_id in due:
            job_id = await self._queue_for(source_id, now)
            if job_id is not None:
                queued += 1
                await publish_sync(self._uow, self._queue, job_id)
        return queued

    async def _queue_for(self, source_id: UUID, now: datetime) -> UUID | None:
        async with self._uow() as uow:
            source = await uow.data_sources.get_for_update(source_id)
            if (
                source is None
                or source.deleted_at is not None
                or source.status is not DataSourceStatus.ACTIVE
                or source.next_sync_at is None
                or source.next_sync_at > now
            ):
                return None
            if source.sync_cursor is None:
                trigger, mode = SyncTrigger.SCHEDULE, first_mode(source)
            elif reconciliation_due(source.last_reconciled_at, now):
                trigger, mode = SyncTrigger.RECONCILIATION, SyncMode.RECONCILIATION
            else:
                trigger, mode = SyncTrigger.SCHEDULE, SyncMode.INCREMENTAL
            job = await add_sync_job(uow, source, trigger=trigger, mode=mode, requested_by=None, now=now)
            await uow.commit()
        return job.id if job is not None else None

    async def _recover(self, now: datetime) -> None:
        async with self._uow() as uow:
            stalled = await uow.sync_jobs.stalled(idle_since=now - STALLED_AFTER, limit=SCHEDULER_BATCH)
            for job_id in stalled:
                job = await uow.sync_jobs.get_for_update(job_id)
                if job is None or job.status is not JobStatus.RUNNING:
                    continue
                if job.attempt >= job.max_attempts:
                    job.status, job.error_code, job.finished_at = JobStatus.DEAD, ATTEMPTS_EXHAUSTED, now
                else:
                    job.status, job.error_code = JobStatus.QUEUED, TRANSIENT
                    await uow.sync_jobs.set_task_id(job.id, None)
                job.updated_at = now
                await uow.sync_jobs.save(job)
            await uow.commit()
        async with self._uow() as uow:
            unpublished = await uow.sync_jobs.unpublished(
                idle_since=now - UNPUBLISHED_AFTER, limit=SCHEDULER_BATCH
            )
        for job_id in unpublished:
            if not await publish_sync(self._uow, self._queue, job_id):
                return
