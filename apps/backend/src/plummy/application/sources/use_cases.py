"""Data sources API use cases (API_CONTRACTS.md §2.9, ADR-020 §1, §6, §7).

Access: ``data_sources.read`` plus ``read`` on the source's knowledge base to see a source (otherwise ``404``,
its existence is not disclosed); ``data_sources.manage`` plus ``manage`` on that knowledge base to change it.
The ``upload`` source of a knowledge base is listed but lives and dies with it (ADR-008).

Tokens: the refresh token is stored only through the vault, encrypted; the access token lives in memory for
one call. Provider calls (token exchange, revoke) run outside database transactions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Final
from uuid import UUID

from plummy.application.audit import by_user
from plummy.application.errors import (
    ConflictError,
    InvalidInputError,
    NotFoundError,
    PermissionDeniedError,
    ServiceUnavailableError,
)
from plummy.application.knowledge.access import require_kb_access
from plummy.application.pagination import Page, PageRequest
from plummy.application.ports import Clock, JobQueue, JobQueueUnavailableError, UnitOfWork, UnitOfWorkFactory
from plummy.application.secret_storage.vault import SecretVault
from plummy.application.security import ClientInfo, SecurityContext
from plummy.application.sources.oauth import (
    STATE_TTL_SECONDS,
    code_challenge,
    new_code_verifier,
    new_state,
    plausible_state,
    state_key,
)
from plummy.application.sources.ports import (
    ConnectorError,
    OAuthClient,
    OAuthClients,
    OAuthStateStore,
    OAuthStateUnavailableError,
    PendingAuthorization,
)
from plummy.core.ids import new_id
from plummy.domain.audit import ActorType, AuditAction, AuditEntry, AuditOutcome
from plummy.domain.documents import DeletionReason
from plummy.domain.identity import Permission
from plummy.domain.knowledge import (
    DataSource,
    DataSourceStatus,
    DataSourceType,
    InvalidKnowledgeInputError,
    KbAccessLevel,
    normalize_kb_name,
)
from plummy.domain.secrets import SecretKind, SecretValue
from plummy.domain.sources import (
    DEFAULT_SYNC_INTERVAL_MINUTES,
    CredentialStatus,
    InvalidSyncIntervalError,
    SourceCredentials,
    check_sync_interval,
    normalize_account_label,
)

logger = logging.getLogger(__name__)

SOURCE_NOT_FOUND: Final = "Data source not found."
CANCELLED_BY_SOURCE_DELETION: Final = "source_deleted"
CANCELLED_BY_DISCONNECT: Final = "source_disconnected"
MAX_CODE_LENGTH: Final = 2048


class _Unset:
    """Marks a PATCH field that was not sent (``None`` is a value: manual sync only)."""


UNSET: Final = _Unset()


class ConnectorNotConfiguredError(ConflictError):
    """The OAuth client of this source type is not configured on the server (Instance Settings)."""

    code = "connector_not_configured"


class UploadSourceManagedByKnowledgeBaseError(ConflictError):
    """The upload source of a knowledge base is created, renamed and deleted with it."""

    code = "upload_source_managed_by_kb"


class OAuthStoreUnavailableError(ServiceUnavailableError):
    """The OAuth state store (Redis) is unavailable."""


@dataclass(frozen=True, slots=True)
class DataSourceView:
    source: DataSource
    knowledge_base_id: UUID
    credentials: SourceCredentials | None


@dataclass(frozen=True, slots=True)
class DataSourceInput:
    type: DataSourceType
    name: str
    knowledge_base_id: UUID
    sync_interval_minutes: int | None = DEFAULT_SYNC_INTERVAL_MINUTES


@dataclass(frozen=True, slots=True)
class DataSourcePatch:
    name: str | None = None
    sync_interval_minutes: int | _Unset | None = UNSET  # ``None`` = manual sync only


def _name(value: str) -> str:
    try:
        return normalize_kb_name(value)
    except InvalidKnowledgeInputError as exc:
        raise InvalidInputError(exc.field, exc.code, str(exc)) from None


def _interval(value: int | None) -> int | None:
    try:
        return check_sync_interval(value)
    except InvalidSyncIntervalError as exc:
        raise InvalidInputError("sync_interval_minutes", "invalid_sync_interval", str(exc)) from None


async def _visible(
    uow: UnitOfWork, context: SecurityContext, source_id: UUID, required: KbAccessLevel, *, lock: bool = False
) -> tuple[DataSource, UUID]:
    """The source and its knowledge base if the caller has ``required`` access to it."""
    source = await (uow.data_sources.get_for_update if lock else uow.data_sources.get)(source_id)
    kb_id = (await uow.data_sources.knowledge_base_of([source_id])).get(source_id) if source else None
    if source is None or source.deleted_at is not None or kb_id is None:
        raise NotFoundError(SOURCE_NOT_FOUND)
    try:
        await require_kb_access(uow, context, kb_id, required)
    except NotFoundError:
        raise NotFoundError(SOURCE_NOT_FOUND) from None
    except PermissionDeniedError:
        raise PermissionDeniedError("You do not have permission to manage this data source.") from None
    return source, kb_id


def _external(source: DataSource) -> None:
    if not source.is_external:
        raise UploadSourceManagedByKnowledgeBaseError()


def _client(clients: OAuthClients, source_type: DataSourceType) -> OAuthClient:
    client = clients.get(source_type)
    if client is None:
        raise ConnectorNotConfiguredError()
    return client


class ListDataSources:
    def __init__(self, uow: UnitOfWorkFactory) -> None:
        self._uow = uow

    async def execute(self, context: SecurityContext, page: PageRequest) -> Page[DataSourceView]:
        context.require(Permission.DATA_SOURCES_READ)
        everything = context.has_permission(Permission.KNOWLEDGE_BASES_MANAGE)
        async with self._uow() as uow:
            sources = await uow.data_sources.page(page, visible_to=None if everything else context.user_id)
            ids = [source.id for source in sources.items]
            kbs = await uow.data_sources.knowledge_base_of(ids)
            views = [
                DataSourceView(source, kbs[source.id], await uow.source_credentials.get(source.id))
                for source in sources.items
                if source.id in kbs
            ]
        return Page(items=views, next_cursor=sources.next_cursor)


class GetDataSource:
    def __init__(self, uow: UnitOfWorkFactory) -> None:
        self._uow = uow

    async def execute(self, context: SecurityContext, source_id: UUID) -> DataSourceView:
        context.require(Permission.DATA_SOURCES_READ)
        async with self._uow() as uow:
            source, kb_id = await _visible(uow, context, source_id, KbAccessLevel.READ)
            return DataSourceView(source, kb_id, await uow.source_credentials.get(source.id))


class CreateDataSource:
    """A new external source of one knowledge base, ``paused`` until connected and a folder is chosen."""

    def __init__(self, uow: UnitOfWorkFactory, clock: Clock, clients: OAuthClients) -> None:
        self._uow, self._clock, self._clients = uow, clock, clients

    async def execute(
        self, context: SecurityContext, client: ClientInfo, data: DataSourceInput
    ) -> DataSourceView:
        context.require(Permission.DATA_SOURCES_MANAGE)
        if data.type is DataSourceType.UPLOAD:
            raise InvalidInputError(
                "type", "source_type_not_allowed", "Upload sources are created with their knowledge base."
            )
        name = _name(data.name)
        interval = _interval(data.sync_interval_minutes)
        _client(self._clients, data.type)
        now = self._clock.now()
        async with self._uow() as uow:
            kb, _ = await require_kb_access(uow, context, data.knowledge_base_id, KbAccessLevel.MANAGE)
            source = DataSource(
                id=new_id(),
                type=data.type,
                name=name,
                status=DataSourceStatus.PAUSED,
                owner_knowledge_base_id=None,
                created_by=context.user_id,
                created_at=now,
                updated_at=now,
                sync_interval_minutes=interval,
            )
            await uow.data_sources.add(source)
            await uow.data_sources.attach(kb.id, source.id, at=now)
            await uow.audit.record(
                by_user(
                    context,
                    client,
                    AuditAction.DATA_SOURCE_CREATED,
                    target_type="data_source",
                    target_id=source.id,
                    details={"type": str(source.type), "knowledge_base_id": str(kb.id)},
                )
            )
            await uow.commit()
        return DataSourceView(source, kb.id, None)


class UpdateDataSource:
    def __init__(self, uow: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def execute(
        self, context: SecurityContext, client: ClientInfo, source_id: UUID, patch: DataSourcePatch
    ) -> DataSourceView:
        context.require(Permission.DATA_SOURCES_MANAGE)
        name = _name(patch.name) if patch.name is not None else None
        interval_changed = not isinstance(patch.sync_interval_minutes, _Unset)
        interval = (
            None
            if isinstance(patch.sync_interval_minutes, _Unset)
            else _interval(patch.sync_interval_minutes)
        )
        now = self._clock.now()
        async with self._uow() as uow:
            source, kb_id = await _visible(uow, context, source_id, KbAccessLevel.MANAGE, lock=True)
            _external(source)
            changed: list[str] = []
            if name is not None and name != source.name:
                source.name = name
                changed.append("name")
            if interval_changed and interval != source.sync_interval_minutes:
                source.sync_interval_minutes = interval
                if interval is None:
                    source.next_sync_at = None
                changed.append("sync_interval_minutes")
            if changed:
                await uow.data_sources.save(source, at=now)
                await uow.audit.record(
                    by_user(
                        context,
                        client,
                        AuditAction.DATA_SOURCE_UPDATED,
                        target_type="data_source",
                        target_id=source.id,
                        details={"fields": ",".join(changed)},
                    )
                )
            credentials = await uow.source_credentials.get(source.id)
            await uow.commit()
        return DataSourceView(source, kb_id, credentials)


async def drop_credentials(
    uow: UnitOfWork, vault: SecretVault, source_id: UUID
) -> tuple[SourceCredentials | None, SecretValue | None]:
    """Deletes the credentials and their secret in the caller's transaction; the token, to revoke after the
    commit (``None`` when it cannot be decrypted any more)."""
    credentials = await uow.source_credentials.get(source_id)
    if credentials is None:
        return None, None
    token: SecretValue | None = None
    try:
        token = await vault.reveal(uow, credentials.secret_ref_id)
    except ServiceUnavailableError:
        logger.warning("refresh token not readable, revoke skipped", extra={"data_source_id": str(source_id)})
    await uow.source_credentials.delete(source_id)
    await uow.secrets.delete(credentials.secret_ref_id)
    return credentials, token


async def revoke_token(client: OAuthClient | None, token: SecretValue | None, source_id: UUID) -> bool:
    """Best effort: a provider failure never blocks the local removal (ADR-020 §7)."""
    if client is None or token is None:
        return False
    try:
        await client.revoke(token)
    except ConnectorError as exc:
        logger.warning(
            "token revocation failed", extra={"data_source_id": str(source_id), "error_code": exc.code}
        )
        return False
    return True


class DisconnectDataSource:
    """Revoke at the provider, delete the credentials, stop syncing; documents stay until the source is
    deleted."""

    def __init__(
        self, uow: UnitOfWorkFactory, clock: Clock, vault: SecretVault, clients: OAuthClients
    ) -> None:
        self._uow, self._clock, self._vault, self._clients = uow, clock, vault, clients

    async def execute(self, context: SecurityContext, client: ClientInfo, source_id: UUID) -> DataSourceView:
        context.require(Permission.DATA_SOURCES_MANAGE)
        now = self._clock.now()
        async with self._uow() as uow:
            source, kb_id = await _visible(uow, context, source_id, KbAccessLevel.MANAGE, lock=True)
            _external(source)
            credentials, token = await drop_credentials(uow, self._vault, source.id)
            cancelled = await uow.sync_jobs.cancel_active(
                source.id, error_code=CANCELLED_BY_DISCONNECT, at=now
            )
            source.status = DataSourceStatus.DISCONNECTED
            source.next_sync_at = None
            source.sync_cursor = None
            await uow.data_sources.save(source, at=now)
            await uow.commit()
        revoked = await revoke_token(self._clients.get(source.type), token, source.id)
        async with self._uow() as uow:
            await uow.audit.record(
                by_user(
                    context,
                    client,
                    AuditAction.DATA_SOURCE_DISCONNECTED,
                    target_type="data_source",
                    target_id=source.id,
                    details={
                        "had_credentials": credentials is not None,
                        "revoked_at_provider": revoked,
                        "sync_jobs_cancelled": cancelled,
                    },
                )
            )
            await uow.commit()
        return DataSourceView(source, kb_id, None)


class DeleteDataSource:
    """Disconnect, then every document of the source is deleted (``source_deleted``) and purged later."""

    def __init__(
        self,
        uow: UnitOfWorkFactory,
        queue: JobQueue,
        clock: Clock,
        vault: SecretVault,
        clients: OAuthClients,
    ) -> None:
        self._uow, self._queue, self._clock, self._vault, self._clients = uow, queue, clock, vault, clients

    async def execute(self, context: SecurityContext, client: ClientInfo, source_id: UUID) -> int:
        context.require(Permission.DATA_SOURCES_MANAGE)
        now = self._clock.now()
        async with self._uow() as uow:
            source, kb_id = await _visible(uow, context, source_id, KbAccessLevel.MANAGE, lock=True)
            _external(source)
            _, token = await drop_credentials(uow, self._vault, source.id)
            await uow.sync_jobs.cancel_active(source.id, error_code=CANCELLED_BY_SOURCE_DELETION, at=now)
            documents = await uow.documents.mark_deleted_in_source(
                source.id, DeletionReason.SOURCE_DELETED, at=now
            )
            cancelled = await uow.ingestion_jobs.cancel_unfinished(
                documents, CANCELLED_BY_SOURCE_DELETION, at=now
            )
            await uow.data_sources.detach(source.id)
            await uow.data_sources.mark_deleted(source.id, at=now)
            await uow.audit.record(
                by_user(
                    context,
                    client,
                    AuditAction.DATA_SOURCE_DELETED,
                    target_type="data_source",
                    target_id=source.id,
                    details={
                        "knowledge_base_id": str(kb_id),
                        "documents": len(documents),
                        "jobs_cancelled": cancelled,
                    },
                )
            )
            await uow.commit()
        await revoke_token(self._clients.get(source.type), token, source.id)
        for document_id in documents:
            try:
                await self._queue.enqueue_purge(document_id)
            except JobQueueUnavailableError:
                logger.warning(
                    "purge not published, broker unavailable", extra={"data_source_id": str(source_id)}
                )
                break  # the sweep purges the rest
        return len(documents)


class StartOAuth:
    """``state`` (single use, bound to the session and the user, 10 minutes) and a PKCE verifier are kept
    server-side; the browser only gets the provider's authorization URL."""

    def __init__(
        self,
        uow: UnitOfWorkFactory,
        states: OAuthStateStore,
        clients: OAuthClients,
        redirect_uri: str | None,
    ) -> None:
        self._uow, self._states, self._clients, self._redirect_uri = uow, states, clients, redirect_uri

    async def execute(self, context: SecurityContext, client: ClientInfo, source_id: UUID) -> str:
        context.require(Permission.DATA_SOURCES_MANAGE)
        async with self._uow() as uow:
            source, _ = await _visible(uow, context, source_id, KbAccessLevel.MANAGE)
            _external(source)
            oauth = _client(self._clients, source.type)
            if self._redirect_uri is None:
                raise ConnectorNotConfiguredError()
            state, verifier = new_state(), new_code_verifier()
            try:
                await self._states.put(
                    state_key(state),
                    PendingAuthorization(
                        data_source_id=source.id,
                        user_id=context.user_id,
                        session_id=context.session_id,
                        code_verifier=verifier,
                    ),
                    ttl_seconds=STATE_TTL_SECONDS,
                )
            except OAuthStateUnavailableError:
                raise OAuthStoreUnavailableError() from None
            await uow.audit.record(
                by_user(
                    context,
                    client,
                    AuditAction.DATA_SOURCE_OAUTH_STARTED,
                    target_type="data_source",
                    target_id=source.id,
                )
            )
            await uow.commit()
        return oauth.authorization_url(
            state=state, code_challenge=code_challenge(verifier), redirect_uri=self._redirect_uri
        )


@dataclass(frozen=True, slots=True)
class CallbackParams:
    state: str | None
    code: str | None
    error: str | None


class _OAuthFailedError(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _ui_path(kb_id: UUID | None, source_id: UUID | None, outcome: str, reason: str | None = None) -> str:
    """Built only from server-side values: the callback is not an open redirect."""
    if kb_id is None or source_id is None:
        path = "/knowledge-bases?oauth=" + outcome
    else:
        path = f"/knowledge-bases/{kb_id}?source={source_id}&oauth={outcome}"
    return path + (f"&reason={reason}" if reason else "")


class CompleteOAuth:
    """``GET /data-sources/oauth/callback``. Every failure ends in a redirect to the UI with a reason code and
    an audit entry; the provider's own error text is never shown or stored."""

    def __init__(
        self,
        uow: UnitOfWorkFactory,
        clock: Clock,
        vault: SecretVault,
        states: OAuthStateStore,
        clients: OAuthClients,
        redirect_uri: str | None,
    ) -> None:
        self._uow, self._clock, self._vault = uow, clock, vault
        self._states, self._clients, self._redirect_uri = states, clients, redirect_uri

    async def execute(
        self, context: SecurityContext | None, client: ClientInfo, params: CallbackParams
    ) -> str:
        if not plausible_state(params.state):
            return _ui_path(None, None, "failed", "oauth_state_invalid")
        assert params.state is not None  # noqa: S101 - checked by plausible_state
        try:
            pending = await self._states.take(state_key(params.state))
        except OAuthStateUnavailableError:
            return _ui_path(None, None, "failed", "oauth_unavailable")
        if pending is None:
            return _ui_path(None, None, "failed", "oauth_state_invalid")
        kb_id: UUID | None = None
        try:
            if (
                context is None
                or context.session_id != pending.session_id
                or context.user_id != pending.user_id
            ):
                raise _OAuthFailedError("oauth_session_mismatch")
            kb_id = await self._authorize(context, pending.data_source_id)
            if params.error is not None:
                raise _OAuthFailedError(
                    "oauth_denied" if params.error == "access_denied" else "oauth_provider_error"
                )
            if not params.code or len(params.code) > MAX_CODE_LENGTH:
                raise _OAuthFailedError("oauth_code_missing")
            await self._connect(context, client, pending, SecretValue(params.code))
        except _OAuthFailedError as failure:
            await self._record_failure(context, client, pending, failure.reason)
            return _ui_path(kb_id, pending.data_source_id if kb_id else None, "failed", failure.reason)
        return _ui_path(kb_id, pending.data_source_id, "connected")

    async def _authorize(self, context: SecurityContext, source_id: UUID) -> UUID:
        """The user must still be allowed to manage the source: rights may have changed since the start."""
        if not context.has_permission(Permission.DATA_SOURCES_MANAGE):
            raise _OAuthFailedError("oauth_forbidden")
        async with self._uow() as uow:
            try:
                source, kb_id = await _visible(uow, context, source_id, KbAccessLevel.MANAGE)
            except (NotFoundError, PermissionDeniedError):
                raise _OAuthFailedError("oauth_forbidden") from None
            if not source.is_external or source.type not in self._clients:
                raise _OAuthFailedError("oauth_forbidden")
        return kb_id

    async def _connect(
        self, context: SecurityContext, client: ClientInfo, pending: PendingAuthorization, code: SecretValue
    ) -> None:
        async with self._uow() as uow:
            source = await uow.data_sources.get(pending.data_source_id)
        assert source is not None  # noqa: S101 - checked by _authorize
        oauth = self._clients[source.type]
        if self._redirect_uri is None:
            raise _OAuthFailedError("oauth_not_configured")
        try:
            grant = await oauth.exchange_code(
                code, code_verifier=SecretValue(pending.code_verifier), redirect_uri=self._redirect_uri
            )
        except ConnectorError as exc:
            raise _OAuthFailedError("oauth_exchange_failed") from exc
        if grant.refresh_token is None:
            await revoke_token(oauth, grant.access_token, source.id)
            raise _OAuthFailedError("oauth_no_refresh_token")
        if not set(oauth.required_scopes) <= set(grant.granted_scopes):
            await revoke_token(oauth, grant.refresh_token, source.id)
            raise _OAuthFailedError("oauth_scope_missing")
        try:
            label = normalize_account_label(await oauth.account_label(grant.access_token))
        except ConnectorError:
            label = None  # the label is for display only

        now = self._clock.now()
        previous: SecretValue | None = None
        async with self._uow() as uow:
            source = await uow.data_sources.get_for_update(pending.data_source_id)
            if source is None or source.deleted_at is not None:
                await revoke_token(oauth, grant.refresh_token, pending.data_source_id)
                raise _OAuthFailedError("oauth_forbidden")
            existing = await uow.source_credentials.get(source.id)
            if existing is not None:
                try:
                    previous = await self._vault.reveal(uow, existing.secret_ref_id)
                except ServiceUnavailableError:
                    previous = None
                record = await self._vault.replace(uow, existing.secret_ref_id, grant.refresh_token)
            else:
                record = await self._vault.store(uow, SecretKind.OAUTH_REFRESH_TOKEN, grant.refresh_token)
            credentials = SourceCredentials(
                data_source_id=source.id,
                secret_ref_id=record.id,
                account_label=label,
                granted_scopes=tuple(sorted(set(grant.granted_scopes))),
                status=CredentialStatus.VALID,
                created_at=existing.created_at if existing else now,
                updated_at=now,
                connected_by=context.user_id,
                last_refreshed_at=now,
            )
            if existing is None:
                await uow.source_credentials.add(credentials)
            else:
                await uow.source_credentials.save(credentials)
            source.status = (
                DataSourceStatus.ACTIVE if source.config.get("folder_id") else DataSourceStatus.PAUSED
            )
            source.last_error = None
            await uow.data_sources.save(source, at=now)
            await uow.audit.record(
                by_user(
                    context,
                    client,
                    AuditAction.DATA_SOURCE_CONNECTED,
                    target_type="data_source",
                    target_id=source.id,
                    details={
                        "scopes": " ".join(credentials.granted_scopes),
                        "reconnected": existing is not None,
                    },
                )
            )
            await uow.commit()
        if previous is not None and previous.reveal() != grant.refresh_token.reveal():
            await revoke_token(oauth, previous, source.id)

    async def _record_failure(
        self,
        context: SecurityContext | None,
        client: ClientInfo,
        pending: PendingAuthorization,
        reason: str,
    ) -> None:
        async with self._uow() as uow:
            if context is not None:
                entry = by_user(
                    context,
                    client,
                    AuditAction.DATA_SOURCE_OAUTH_FAILED,
                    target_type="data_source",
                    target_id=pending.data_source_id,
                    details={"reason": reason},
                    outcome=AuditOutcome.DENIED
                    if reason == "oauth_session_mismatch"
                    else AuditOutcome.FAILURE,
                )
            else:
                entry = AuditEntry(
                    action=AuditAction.DATA_SOURCE_OAUTH_FAILED,
                    outcome=AuditOutcome.DENIED,
                    actor_type=ActorType.ANONYMOUS,
                    target_type="data_source",
                    target_id=pending.data_source_id,
                    ip_address=client.ip_address,
                    user_agent=client.user_agent,
                    details={"reason": reason},
                )
            await uow.audit.record(entry)
            await uow.commit()
