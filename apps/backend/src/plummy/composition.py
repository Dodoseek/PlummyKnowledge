"""Composition root: wires infrastructure adapters into application use cases (ARCHITECTURE.md §7).

Used by the API lifespan now and by Celery workers later, so both roles share the same wiring.
"""

from __future__ import annotations

import importlib.metadata
import logging
import socket
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from urllib.parse import urlsplit

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from plummy.application.assistants.use_cases import (
    CreateAssistant,
    DeleteAssistant,
    GetAssistant,
    ListAssistants,
    UpdateAssistant,
)
from plummy.application.auth.account import ChangePassword, ListMySessions, RevokeMySession
from plummy.application.auth.activation import ActivateAccount
from plummy.application.auth.sessions import SessionIssuer, SessionPolicy
from plummy.application.auth.throttle import AttemptThrottle
from plummy.application.auth.use_cases import AuthenticateSession, Login, Logout
from plummy.application.chat.conversations import (
    CreateConversation,
    DeleteConversation,
    GetConversation,
    ListConversations,
    ListMessages,
    RenameConversation,
)
from plummy.application.chat.runs import StopGeneration, SweepChat
from plummy.application.chat.send import SendMessage
from plummy.application.documents.upload import UploadDocument
from plummy.application.documents.use_cases import (
    DeleteDocument,
    GetDocument,
    ListDocuments,
    ListDocumentVersions,
    ReindexDocument,
    ReindexKnowledgeBase,
)
from plummy.application.documents.viewer import ViewSource
from plummy.application.evaluation.runner import RunRetrievalEvaluation
from plummy.application.groups.use_cases import (
    CreateGroup,
    DeleteGroup,
    GetGroup,
    ListGroups,
    SetGroupMembers,
    SetGroupRoles,
    UpdateGroup,
)
from plummy.application.health import CheckReadiness
from plummy.application.ingestion.indexing import RunIndexingJob
from plummy.application.ingestion.maintenance import (
    CleanupQuarantine,
    PurgeDocument,
    RequeueJobs,
    SweepDeletedDocuments,
)
from plummy.application.ingestion.parsing import ParserRegistry
from plummy.application.ingestion.pipeline import RunIngestionJob
from plummy.application.jobs.use_cases import GetJob, GetSyncJob, ListJobs, ListSyncJobs, RetryJob
from plummy.application.knowledge.use_cases import (
    CreateKnowledgeBase,
    DeleteKnowledgeBase,
    GetKnowledgeBase,
    GetKnowledgeBaseAccess,
    ListKnowledgeBases,
    ReplaceKnowledgeBaseAccess,
    UpdateKnowledgeBase,
)
from plummy.application.knowledge.vector_index import EnsureVectorIndex
from plummy.application.maintenance.deletions import ExportDeletions, ReapplyDeletions
from plummy.application.maintenance.objects import ExportObjects, ImportObjects
from plummy.application.ports import Clock, PasswordHasher, SecretCipher
from plummy.application.providers.embedding_profiles import (
    CreateEmbeddingProfile,
    GetEmbeddingProfile,
    ListEmbeddingProfiles,
    TestEmbeddingProfile,
    UpdateEmbeddingProfile,
)
from plummy.application.providers.use_cases import (
    CreateModel,
    CreateProvider,
    DeleteModel,
    DeleteProvider,
    GetProvider,
    ListModels,
    ListProviders,
    TestProvider,
    UpdateModel,
    UpdateProvider,
)
from plummy.application.retrieval.use_cases import RetrieveChunks, SearchKnowledgeBase
from plummy.application.roles.use_cases import ListPermissions, ListRoles
from plummy.application.secret_storage.use_cases import GetSecretsStatus, RotateSecrets
from plummy.application.secret_storage.vault import SecretVault
from plummy.application.setup.use_cases import (
    CreateAdministrator,
    GetSetupStatus,
    InitializeInstance,
    IssueBootstrapToken,
    ResetPassword,
)
from plummy.application.sources.ports import DataSourceConnector, OAuthClient
from plummy.application.sources.sync import RunSyncJob, ScheduleSyncs
from plummy.application.sources.use_cases import (
    BrowseDataSource,
    CompleteOAuth,
    CreateDataSource,
    DeleteDataSource,
    DisconnectDataSource,
    GetDataSource,
    ListDataSources,
    RequestSync,
    StartOAuth,
    UpdateDataSource,
    ValidateDataSource,
)
from plummy.application.system.use_cases import (
    AllowlistEntryView,
    GetInstanceSettings,
    InstanceSettingsView,
    SecretsSettingsView,
)
from plummy.application.users.use_cases import (
    ActivationLinks,
    CreateUser,
    DeleteUser,
    GetUser,
    ListUsers,
    ReissueActivation,
    RevokeUserSessions,
    SetUserRoles,
    UpdateUser,
)
from plummy.core.config import ConfigurationError, Settings, SetupMode, StorageBackend
from plummy.core.instance_config import InstanceConfig, build_egress_policy, load_instance_config
from plummy.core.network.addresses import IPAddress
from plummy.core.network.errors import EgressDeniedError
from plummy.core.network.policy import EgressPolicy, EgressPurpose
from plummy.core.network.urls import parse_ip
from plummy.domain.file_types import DocumentFormat
from plummy.domain.knowledge import DataSourceType
from plummy.domain.secrets import SecretValue
from plummy.infrastructure.chunking.structured import StructuredChunker
from plummy.infrastructure.clock import SystemClock
from plummy.infrastructure.db import create_engine, create_session_factory
from plummy.infrastructure.db.checks import DatabaseCheck, MigrationsCheck, SecretsCheck
from plummy.infrastructure.db.migrations import get_expected_heads
from plummy.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWorkFactory
from plummy.infrastructure.db.vector_index import PgVectorIndexManager
from plummy.infrastructure.http.egress_audit import EgressDenialAuditor
from plummy.infrastructure.http.safe_client import SafeHttpClient
from plummy.infrastructure.integrations.ai.factory import HttpProviderGateway
from plummy.infrastructure.integrations.sources.google_common import GoogleEndpoints
from plummy.infrastructure.integrations.sources.google_drive import GoogleDriveConnector
from plummy.infrastructure.integrations.sources.google_oauth import GoogleOAuthClient
from plummy.infrastructure.integrations.sources.microsoft_common import MicrosoftEndpoints
from plummy.infrastructure.integrations.sources.microsoft_graph import MicrosoftGraphConnector
from plummy.infrastructure.integrations.sources.microsoft_oauth import MicrosoftOAuthClient
from plummy.infrastructure.parsing.isolated import IsolatedParser
from plummy.infrastructure.parsing.text import PlainTextParser
from plummy.infrastructure.queue.celery_queue import CeleryJobQueue
from plummy.infrastructure.redis import create_redis_client
from plummy.infrastructure.redis.checks import RedisCheck
from plummy.infrastructure.redis.generation_control import RedisGenerationControl
from plummy.infrastructure.redis.oauth_state import RedisOAuthStateStore
from plummy.infrastructure.redis.rate_limiter import RedisRateLimiter
from plummy.infrastructure.security.bootstrap_store import FileBootstrapTokenStore
from plummy.infrastructure.security.passwords import Argon2PasswordHasher
from plummy.infrastructure.security.secret_cipher import AesGcmSecretCipher, KeyRingError, load_keyring
from plummy.infrastructure.storage.local import LocalFileStorage, StorageCheck
from plummy.infrastructure.storage.s3 import S3Storage, S3StorageCheck
from plummy.infrastructure.storage.sigv4 import Credentials
from plummy.workers.app import DEFAULT_NAMESPACE

logger = logging.getLogger(__name__)
API_CONNECTOR_ATTEMPTS = 2  # a request waits for the provider: little room for backoff
WORKER_CONNECTOR_ATTEMPTS = 5  # per request in a sync job (ADR-020 §8)


@dataclass(frozen=True, slots=True)
class AuthServices:
    """Setup and authentication use cases used by the API."""

    authenticate: AuthenticateSession
    login: Login
    logout: Logout
    setup_status: GetSetupStatus
    initialize: InitializeInstance
    activate: ActivateAccount
    list_sessions: ListMySessions
    revoke_session: RevokeMySession
    change_password: ChangePassword


@dataclass(frozen=True, slots=True)
class UserServices:
    list: ListUsers
    get: GetUser
    create: CreateUser
    update: UpdateUser
    delete: DeleteUser
    set_roles: SetUserRoles
    revoke_sessions: RevokeUserSessions
    reissue_activation: ReissueActivation


@dataclass(frozen=True, slots=True)
class GroupServices:
    list: ListGroups
    get: GetGroup
    create: CreateGroup
    update: UpdateGroup
    delete: DeleteGroup
    set_members: SetGroupMembers
    set_roles: SetGroupRoles


@dataclass(frozen=True, slots=True)
class ProviderServices:
    list: ListProviders
    get: GetProvider
    create: CreateProvider
    update: UpdateProvider
    delete: DeleteProvider
    test: TestProvider
    list_models: ListModels
    create_model: CreateModel
    update_model: UpdateModel
    delete_model: DeleteModel


@dataclass(frozen=True, slots=True)
class EmbeddingProfileServices:
    list: ListEmbeddingProfiles
    get: GetEmbeddingProfile
    create: CreateEmbeddingProfile
    update: UpdateEmbeddingProfile
    test: TestEmbeddingProfile


@dataclass(frozen=True, slots=True)
class KnowledgeServices:
    """Knowledge bases; access to each one is checked by the use cases."""

    list: ListKnowledgeBases
    get: GetKnowledgeBase
    create: CreateKnowledgeBase
    update: UpdateKnowledgeBase
    delete: DeleteKnowledgeBase
    access: GetKnowledgeBaseAccess
    replace_access: ReplaceKnowledgeBaseAccess
    retrieve: RetrieveChunks
    search: SearchKnowledgeBase


@dataclass(frozen=True, slots=True)
class AssistantServices:
    """Assistants; who may see or use one is decided by the use cases (ADR-005)."""

    list: ListAssistants
    get: GetAssistant
    create: CreateAssistant
    update: UpdateAssistant
    delete: DeleteAssistant


@dataclass(frozen=True, slots=True)
class ChatServices:
    send: SendMessage
    stop: StopGeneration


@dataclass(frozen=True, slots=True)
class ConversationServices:
    """Conversations; each is private to its owner (API_CONTRACTS.md §2.13)."""

    list: ListConversations
    create: CreateConversation
    get: GetConversation
    rename: RenameConversation
    delete: DeleteConversation
    messages: ListMessages


@dataclass(frozen=True, slots=True)
class DocumentServices:
    """Documents; access to the knowledge base is checked by the use cases."""

    upload: UploadDocument
    list: ListDocuments
    get: GetDocument
    versions: ListDocumentVersions
    delete: DeleteDocument
    reindex: ReindexDocument
    reindex_knowledge_base: ReindexKnowledgeBase
    view: ViewSource


@dataclass(frozen=True, slots=True)
class SourceServices:
    """External document sources (ADR-020); access is checked by the use cases."""

    list: ListDataSources
    get: GetDataSource
    create: CreateDataSource
    update: UpdateDataSource
    delete: DeleteDataSource
    disconnect: DisconnectDataSource
    start_oauth: StartOAuth
    complete_oauth: CompleteOAuth
    validate: ValidateDataSource
    browse: BrowseDataSource
    sync: RequestSync


@dataclass(frozen=True, slots=True)
class JobServices:
    """Background jobs (``jobs.read`` / ``jobs.manage``, checked by the use cases)."""

    list: ListJobs
    get: GetJob
    retry: RetryJob
    list_sync: ListSyncJobs
    get_sync: GetSyncJob


@dataclass(frozen=True, slots=True)
class AdminServices:
    """Administration use cases; each one checks the caller's permissions itself."""

    users: UserServices
    groups: GroupServices
    providers: ProviderServices
    embedding_profiles: EmbeddingProfileServices
    roles: ListRoles
    permissions: ListPermissions
    instance_settings: GetInstanceSettings


@dataclass(frozen=True, slots=True)
class Resources:
    """Process-wide resources owned by the application lifespan."""

    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    redis: Redis
    readiness: CheckReadiness
    auth: AuthServices
    admin: AdminServices
    knowledge: KnowledgeServices
    assistants: AssistantServices
    conversations: ConversationServices
    chat: ChatServices
    documents: DocumentServices
    sources: SourceServices
    job_admin: JobServices
    secrets: SecretVault
    http: SafeHttpClient
    storage: DocumentStorage
    jobs: CeleryJobQueue

    async def aclose(self) -> None:
        """Release connections. Each resource is closed even if another one fails to close."""
        try:
            self.jobs.close()
        except Exception:
            logger.exception("failed to close job queue")
        try:
            await self.http.aclose()
        except Exception:
            logger.exception("failed to close outbound http client")
        try:
            await self.storage.aclose()
        except Exception:
            logger.exception("failed to close document storage")
        try:
            await self.redis.aclose()
        except Exception:
            logger.exception("failed to close redis client")
        try:
            await self.engine.dispose()
        except Exception:
            logger.exception("failed to dispose database engine")


def create_parsers() -> ParserRegistry:
    """Plain text in-process; PDF, DOCX and XLSX in an isolated child process (worker-ingestion image)."""
    try:
        docling = f"docling/{importlib.metadata.version('docling')}"
    except importlib.metadata.PackageNotFoundError:
        docling = "docling/unavailable"  # the child reports parse_failed outside the ingestion image
    isolated = IsolatedParser(
        docling,
        frozenset({DocumentFormat.PDF, DocumentFormat.DOCX, DocumentFormat.XLSX}),
        scratch_dir=Path(tempfile.gettempdir()) / "plummy-parse",
    )
    return ParserRegistry([PlainTextParser(), isolated])


@asynccontextmanager
async def ingestion_runner(settings: Settings) -> AsyncIterator[RunIngestionJob]:
    """Extract phase for one task run (worker-ingestion): its own engine, the asyncpg pool belongs to the
    event loop of the run. Needs no secrets and no outbound network."""
    engine = create_engine(settings, application_name="plummy-worker-ingestion")
    jobs = CeleryJobQueue(settings)
    storage = create_storage(settings)
    try:
        yield RunIngestionJob(
            SqlAlchemyUnitOfWorkFactory(create_session_factory(engine)),
            storage,
            SystemClock(),
            create_parsers(),
            jobs,
        )
    finally:
        jobs.close()
        await storage.aclose()
        await engine.dispose()


@asynccontextmanager
async def indexing_runner(settings: Settings) -> AsyncIterator[RunIndexingJob]:
    """Index phase for one task run (the worker with egress): providers through ``SafeHttpClient``."""
    clock = SystemClock()
    vault = SecretVault(load_secret_cipher(settings), clock)
    policy = load_egress_policy(settings)
    engine = create_engine(settings, application_name="plummy-worker")
    uow = SqlAlchemyUnitOfWorkFactory(create_session_factory(engine))
    http = SafeHttpClient(policy, on_denied=EgressDenialAuditor(uow))
    jobs = CeleryJobQueue(settings)
    storage = create_storage(settings)
    try:
        yield RunIndexingJob(
            uow,
            storage,
            clock,
            chunker=StructuredChunker(),
            gateway=HttpProviderGateway(http),
            vault=vault,
            indexes=PgVectorIndexManager(engine),
            jobs=jobs,
        )
    finally:
        jobs.close()
        await storage.aclose()
        await http.aclose()
        await engine.dispose()


@asynccontextmanager
async def vector_index_runner(settings: Settings) -> AsyncIterator[EnsureVectorIndex]:
    engine = create_engine(settings, application_name="plummy-worker")
    try:
        yield EnsureVectorIndex(
            SqlAlchemyUnitOfWorkFactory(create_session_factory(engine)), PgVectorIndexManager(engine)
        )
    finally:
        await engine.dispose()


@asynccontextmanager
async def sync_runner(settings: Settings) -> AsyncIterator[RunSyncJob]:
    """One sync job (the worker on the ``connectors`` queue): the provider through ``SafeHttpClient``, the
    refresh token from the vault, the downloaded files into quarantine for the ingestion pipeline."""
    clock = SystemClock()
    vault = SecretVault(load_secret_cipher(settings), clock)
    policy = load_egress_policy(settings)
    engine = create_engine(settings, application_name="plummy-worker")
    uow = SqlAlchemyUnitOfWorkFactory(create_session_factory(engine))
    http = SafeHttpClient(policy, on_denied=EgressDenialAuditor(uow))
    jobs = CeleryJobQueue(settings)
    storage = create_storage(settings)
    try:
        yield RunSyncJob(
            uow,
            storage,
            clock,
            vault,
            oauth_clients(settings, http),
            source_connectors(settings, http, max_attempts=WORKER_CONNECTOR_ATTEMPTS),
            jobs,
            max_bytes=settings.upload_max_bytes,
        )
    finally:
        jobs.close()
        await storage.aclose()
        await http.aclose()
        await engine.dispose()


@dataclass(frozen=True, slots=True)
class MaintenanceServices:
    purge: PurgeDocument
    sweep_deleted: SweepDeletedDocuments
    requeue: RequeueJobs
    cleanup_quarantine: CleanupQuarantine
    sweep_chat: SweepChat
    schedule_syncs: ScheduleSyncs


@asynccontextmanager
async def maintenance_runner(settings: Settings) -> AsyncIterator[MaintenanceServices]:
    """Purge and the periodic sweeps for one task run (the worker on the ``maintenance`` queue)."""
    clock = SystemClock()
    storage = create_storage(settings)
    engine = create_engine(settings, application_name="plummy-worker")
    uow = SqlAlchemyUnitOfWorkFactory(create_session_factory(engine))
    jobs = CeleryJobQueue(settings)
    try:
        yield MaintenanceServices(
            purge=PurgeDocument(uow, storage, clock),
            sweep_deleted=SweepDeletedDocuments(uow, jobs, clock),
            requeue=RequeueJobs(uow, jobs, clock),
            cleanup_quarantine=CleanupQuarantine(storage, clock, ttl=settings.quarantine_ttl),
            sweep_chat=SweepChat(uow, clock, timeout_seconds=settings.chat_generation_timeout_seconds),
            schedule_syncs=ScheduleSyncs(uow, jobs, clock),
        )
    finally:
        jobs.close()
        await storage.aclose()
        await engine.dispose()


DocumentStorage = LocalFileStorage | S3Storage


def create_storage(settings: Settings) -> DocumentStorage:
    """Document storage (ARCHITECTURE.md §8.5): a local directory or an S3-compatible bucket (ADR-022)."""
    if settings.storage_backend is not StorageBackend.S3:
        return LocalFileStorage(settings.storage_path)
    policy = load_egress_policy(settings)
    endpoint, bucket, credentials = _s3_settings(settings, policy)
    return S3Storage(
        SafeHttpClient(policy),
        endpoint_url=endpoint,
        bucket=bucket,
        region=settings.s3_region,
        prefix=settings.s3_prefix,
        credentials=credentials,
    )


def check_storage_configuration(settings: Settings) -> None:
    """Startup check (``python -m plummy.check_config``): the S3 endpoint passes the egress policy."""
    if settings.storage_backend is StorageBackend.S3:
        _s3_settings(settings, load_egress_policy(settings))


def _s3_settings(settings: Settings, policy: EgressPolicy) -> tuple[str, str, Credentials]:
    """The S3 endpoint must pass the egress policy (purpose ``storage``); otherwise the process stops."""
    endpoint, bucket = settings.s3_endpoint_url, settings.s3_bucket
    access_key, secret = settings.s3_access_key_id, settings.s3_secret_access_key
    if endpoint is None or bucket is None or access_key is None or secret is None:  # Settings checks them
        raise ConfigurationError("STORAGE_BACKEND=s3 requires the S3_* settings")
    try:
        policy.check_url(endpoint, EgressPurpose.STORAGE, None)
    except EgressDeniedError as exc:
        raise ConfigurationError(
            f"S3_ENDPOINT_URL is not reachable under the network policy ({exc.reason.value}): plain http and "
            'internal addresses need a [[network.allowlist]] entry with purposes = ["storage"]'
        ) from None
    return endpoint, bucket, Credentials(access_key, secret.get_secret_value())


def storage_check(storage: DocumentStorage) -> StorageCheck | S3StorageCheck:
    return S3StorageCheck(storage) if isinstance(storage, S3Storage) else StorageCheck(storage)


def session_policy(settings: Settings) -> SessionPolicy:
    return SessionPolicy(idle_ttl=settings.session_idle_ttl, absolute_ttl=settings.session_absolute_ttl)


def load_secret_cipher(settings: Settings) -> AesGcmSecretCipher | None:
    """The key ring is read once per process; an invalid one stops the API, worker and CLI (ADR-016 §2)."""
    if settings.secrets_keyring_file is None:
        return None
    try:
        return AesGcmSecretCipher(load_keyring(settings.secrets_keyring_file))
    except KeyRingError as exc:
        raise ConfigurationError(f"Invalid configuration: SECRETS_KEYRING_FILE: {exc}") from None


def _platform_addresses(settings: Settings) -> list[tuple[str, IPAddress]]:
    """Addresses of PostgreSQL and Redis, which an allowlist CIDR must never include (ADR-007 §3)."""
    services = {
        "PostgreSQL": urlsplit(settings.database_url.get_secret_value()).hostname,
        "Redis": urlsplit(settings.redis_url.get_secret_value()).hostname,
    }
    addresses: list[tuple[str, IPAddress]] = []
    for service, host in services.items():
        if not host:
            continue
        literal = parse_ip(host)
        if literal is not None:
            addresses.append((service, literal))
            continue
        try:
            infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except OSError:
            logger.warning("cannot resolve %s host to check the network allowlist", service)
            continue
        for info in infos:
            address = parse_ip(str(info[4][0]).split("%", 1)[0])
            if address is not None:
                addresses.append((service, address))
    return addresses


def load_config(settings: Settings) -> InstanceConfig:
    """CONFIG_FILE; replacement provider endpoints are refused in production (ADR-020 §2, ADR-021 §2)."""
    config = load_instance_config(settings.config_file)
    for name, section in (("google_drive", config.google_drive), ("microsoft_365", config.microsoft_365)):
        if settings.is_production and section is not None:
            raise ConfigurationError(
                f"Invalid configuration: CONFIG_FILE: [connectors.{name}] "
                "is not allowed when APP_ENV=production"
            )
    return config


def oauth_clients(settings: Settings, http: SafeHttpClient) -> dict[DataSourceType, OAuthClient]:
    """Configured OAuth clients of external sources; an unconfigured type is simply absent."""
    clients: dict[DataSourceType, OAuthClient] = {}
    if settings.google_oauth_client_id is not None and settings.google_oauth_client_secret is not None:
        clients[DataSourceType.GOOGLE_DRIVE] = GoogleOAuthClient(
            http,
            client_id=settings.google_oauth_client_id,
            client_secret=SecretValue(settings.google_oauth_client_secret.get_secret_value()),
            endpoints=GoogleEndpoints.from_config(load_config(settings).google_drive),
        )
    if (
        settings.microsoft_oauth_tenant_id is not None
        and settings.microsoft_oauth_client_id is not None
        and settings.microsoft_oauth_client_secret is not None
    ):
        clients[DataSourceType.MICROSOFT_365] = MicrosoftOAuthClient(
            http,
            client_id=settings.microsoft_oauth_client_id,
            client_secret=SecretValue(settings.microsoft_oauth_client_secret.get_secret_value()),
            endpoints=MicrosoftEndpoints.for_tenant(
                settings.microsoft_oauth_tenant_id, load_config(settings).microsoft_365
            ),
        )
    return clients


def source_connectors(
    settings: Settings, http: SafeHttpClient, *, max_attempts: int
) -> dict[DataSourceType, DataSourceConnector]:
    """Connectors of the configured source types; ``max_attempts`` per request (few in the API process)."""
    connectors: dict[DataSourceType, DataSourceConnector] = {}
    if settings.google_drive_configured:
        connectors[DataSourceType.GOOGLE_DRIVE] = GoogleDriveConnector(
            http, GoogleEndpoints.from_config(load_config(settings).google_drive), max_attempts=max_attempts
        )
    if settings.microsoft_365_configured and settings.microsoft_oauth_tenant_id is not None:
        endpoints = MicrosoftEndpoints.for_tenant(
            settings.microsoft_oauth_tenant_id, load_config(settings).microsoft_365
        )
        connectors[DataSourceType.MICROSOFT_365] = MicrosoftGraphConnector(
            http, endpoints, max_attempts=max_attempts
        )
    return connectors


def load_egress_policy(settings: Settings) -> EgressPolicy:
    """The outbound network policy from CONFIG_FILE; an invalid file stops the process (ADR-007)."""
    config = load_config(settings)
    needs_platform_check = any(entry.allowed_cidrs for entry in config.allowlist)
    return build_egress_policy(config, _platform_addresses(settings) if needs_platform_check else ())


def instance_settings_view(
    settings: Settings, cipher: SecretCipher | None, policy: EgressPolicy | None = None
) -> InstanceSettingsView:
    return InstanceSettingsView(
        app_env=settings.app_env.value,
        public_url=settings.public_url,
        setup_mode=settings.setup_mode.value,
        session_idle_ttl=settings.session_idle_ttl,
        session_absolute_ttl=settings.session_absolute_ttl,
        session_cookie_secure=settings.session_cookie_secure,
        api_docs_enabled=settings.docs_enabled,
        secrets=SecretsSettingsView(
            configured=cipher is not None,
            primary_key_id=cipher.primary_key_id if cipher else None,
            keys=len(cipher.key_ids) if cipher else 0,
        ),
        google_drive_configured=settings.google_drive_configured,
        microsoft_365_configured=settings.microsoft_365_configured,
        oauth_redirect_uri=settings.oauth_redirect_uri,
        network_allowlist=tuple(
            AllowlistEntryView(
                name=entry.name,
                url=entry.url.origin + entry.path_prefix,
                purposes=tuple(sorted(purpose.value for purpose in entry.purposes)),
                provider_types=tuple(sorted(entry.provider_types))
                if entry.provider_types is not None
                else None,
                allowed_cidrs=tuple(str(network) for network in entry.allowed_cidrs),
            )
            for entry in (policy.allowlist if policy else ())
        ),
    )


def build_resources(
    settings: Settings,
    *,
    password_hasher: PasswordHasher | None = None,
    clock: Clock | None = None,
    job_namespace: str = DEFAULT_NAMESPACE,
) -> Resources:
    """Create clients without connecting; connectivity is verified by readiness checks."""
    cipher = load_secret_cipher(settings)
    egress_policy = load_egress_policy(settings)
    expected_heads = get_expected_heads()
    engine = create_engine(settings)
    redis = create_redis_client(settings)
    storage = create_storage(settings)
    jobs = CeleryJobQueue(settings, namespace=job_namespace)
    readiness = CheckReadiness(
        [
            DatabaseCheck(engine),
            MigrationsCheck(engine, expected_heads),
            RedisCheck(redis),
            SecretsCheck(engine, cipher.key_ids if cipher else frozenset()),
            storage_check(storage),
        ]
    )
    session_factory = create_session_factory(engine)
    uow = SqlAlchemyUnitOfWorkFactory(session_factory)
    clock = clock or SystemClock()
    hasher = password_hasher or Argon2PasswordHasher()
    limiter = RedisRateLimiter(redis)
    throttle = AttemptThrottle(limiter)
    vault = SecretVault(cipher, clock)
    http = SafeHttpClient(egress_policy, on_denied=EgressDenialAuditor(uow))
    gateway = HttpProviderGateway(http)
    retrieve = RetrieveChunks(uow, gateway, vault)
    generation_control = RedisGenerationControl(redis)
    clients = oauth_clients(settings, http)
    connectors = source_connectors(settings, http, max_attempts=API_CONNECTOR_ATTEMPTS)
    oauth_states = RedisOAuthStateStore(redis)
    redirect_uri = settings.oauth_redirect_uri
    chat_timeout = settings.chat_generation_timeout_seconds
    policy = session_policy(settings)
    issuer = SessionIssuer(policy)
    web_setup_enabled = settings.setup_mode is SetupMode.WEB
    auth = AuthServices(
        authenticate=AuthenticateSession(uow, clock, policy),
        login=Login(uow, clock, hasher, throttle, issuer),
        logout=Logout(uow, clock),
        setup_status=GetSetupStatus(uow, web_setup_enabled=web_setup_enabled),
        initialize=InitializeInstance(
            uow, clock, hasher, throttle, issuer, web_setup_enabled=web_setup_enabled
        ),
        activate=ActivateAccount(uow, clock, hasher, throttle, issuer),
        list_sessions=ListMySessions(uow, clock, issuer),
        revoke_session=RevokeMySession(uow, clock),
        change_password=ChangePassword(uow, clock, hasher, throttle, issuer),
    )
    links = ActivationLinks(settings.public_url)
    admin = AdminServices(
        users=UserServices(
            list=ListUsers(uow),
            get=GetUser(uow),
            create=CreateUser(uow, clock, links),
            update=UpdateUser(uow, clock),
            delete=DeleteUser(uow, clock),
            set_roles=SetUserRoles(uow),
            revoke_sessions=RevokeUserSessions(uow, clock),
            reissue_activation=ReissueActivation(uow, clock, links),
        ),
        groups=GroupServices(
            list=ListGroups(uow),
            get=GetGroup(uow),
            create=CreateGroup(uow, clock),
            update=UpdateGroup(uow, clock),
            delete=DeleteGroup(uow, clock),
            set_members=SetGroupMembers(uow),
            set_roles=SetGroupRoles(uow),
        ),
        providers=ProviderServices(
            list=ListProviders(uow),
            get=GetProvider(uow),
            create=CreateProvider(uow, clock, vault, gateway),
            update=UpdateProvider(uow, clock, vault, gateway),
            delete=DeleteProvider(uow, clock),
            test=TestProvider(uow, clock, vault, gateway, limiter),
            list_models=ListModels(uow),
            create_model=CreateModel(uow, clock),
            update_model=UpdateModel(uow, clock),
            delete_model=DeleteModel(uow),
        ),
        embedding_profiles=EmbeddingProfileServices(
            list=ListEmbeddingProfiles(uow),
            get=GetEmbeddingProfile(uow),
            create=CreateEmbeddingProfile(uow, clock),
            update=UpdateEmbeddingProfile(uow, clock),
            test=TestEmbeddingProfile(uow, clock, vault, gateway, limiter),
        ),
        roles=ListRoles(uow),
        permissions=ListPermissions(uow),
        instance_settings=GetInstanceSettings(instance_settings_view(settings, cipher, egress_policy)),
    )
    return Resources(
        engine=engine,
        session_factory=session_factory,
        redis=redis,
        readiness=readiness,
        auth=auth,
        admin=admin,
        knowledge=KnowledgeServices(
            list=ListKnowledgeBases(uow),
            get=GetKnowledgeBase(uow),
            create=CreateKnowledgeBase(uow, clock, jobs),
            update=UpdateKnowledgeBase(uow, clock),
            delete=DeleteKnowledgeBase(uow, jobs, clock, vault, clients),
            access=GetKnowledgeBaseAccess(uow),
            replace_access=ReplaceKnowledgeBaseAccess(uow, clock),
            retrieve=retrieve,
            search=SearchKnowledgeBase(uow, retrieve, limiter),
        ),
        assistants=AssistantServices(
            list=ListAssistants(uow),
            get=GetAssistant(uow),
            create=CreateAssistant(uow, clock),
            update=UpdateAssistant(uow, clock),
            delete=DeleteAssistant(uow, clock),
        ),
        conversations=ConversationServices(
            list=ListConversations(uow),
            create=CreateConversation(uow, clock),
            get=GetConversation(uow),
            rename=RenameConversation(uow, clock),
            delete=DeleteConversation(uow, clock),
            messages=ListMessages(uow),
        ),
        chat=ChatServices(
            send=SendMessage(
                uow,
                retrieve,
                gateway,
                vault,
                limiter,
                generation_control,
                clock,
                timeout_seconds=chat_timeout,
            ),
            stop=StopGeneration(uow, generation_control, timeout_seconds=chat_timeout),
        ),
        documents=DocumentServices(
            upload=UploadDocument(uow, storage, jobs, limiter, clock, max_bytes=settings.upload_max_bytes),
            list=ListDocuments(uow),
            get=GetDocument(uow),
            versions=ListDocumentVersions(uow),
            delete=DeleteDocument(uow, jobs, clock),
            reindex=ReindexDocument(uow, jobs, limiter, clock),
            reindex_knowledge_base=ReindexKnowledgeBase(uow, jobs, limiter, clock),
            view=ViewSource(uow),
        ),
        sources=SourceServices(
            list=ListDataSources(uow),
            get=GetDataSource(uow),
            create=CreateDataSource(uow, clock, clients),
            update=UpdateDataSource(uow, clock, vault, clients, connectors, jobs),
            delete=DeleteDataSource(uow, jobs, clock, vault, clients),
            disconnect=DisconnectDataSource(uow, clock, vault, clients),
            start_oauth=StartOAuth(uow, oauth_states, clients, redirect_uri),
            complete_oauth=CompleteOAuth(uow, clock, vault, oauth_states, clients, redirect_uri, jobs),
            validate=ValidateDataSource(uow, clock, vault, clients, connectors, limiter),
            browse=BrowseDataSource(uow, clock, vault, clients, connectors),
            sync=RequestSync(uow, jobs, limiter, clock),
        ),
        job_admin=JobServices(
            list=ListJobs(uow),
            get=GetJob(uow),
            retry=RetryJob(uow, jobs, limiter, clock),
            list_sync=ListSyncJobs(uow),
            get_sync=GetSyncJob(uow),
        ),
        secrets=vault,
        http=http,
        storage=storage,
        jobs=jobs,
    )


@dataclass(frozen=True, slots=True)
class EvaluationServices:
    """``python -m plummy.cli eval``: the full application resources (database, Redis, job queue, providers),
    because the run goes through upload, the workers and retrieval exactly like the web application."""

    resources: Resources
    uow: SqlAlchemyUnitOfWorkFactory
    clock: Clock
    run: RunRetrievalEvaluation

    async def aclose(self) -> None:
        await self.resources.aclose()


class RestoreTask(StrEnum):
    EXPORT_DELETIONS = "export-deletions"
    REAPPLY_DELETIONS = "reapply-deletions"
    OBJECTS = "objects"


@dataclass(frozen=True, slots=True)
class RestoreServices:
    """``python -m plummy.cli maintenance`` (ADR-023 §6), built for one task. Exporting deletions needs only
    the database (it runs on the source instance before a restore); objects need only the storage;
    reapplying deletions uses the application's resources: the job queue for purges, the key ring and the
    OAuth clients to drop and revoke credentials."""

    export_deletions: ExportDeletions | None = None
    reapply_deletions: ReapplyDeletions | None = None
    export_objects: ExportObjects | None = None
    import_objects: ImportObjects | None = None
    engine: AsyncEngine | None = None
    storage: DocumentStorage | None = None
    resources: Resources | None = None

    async def aclose(self) -> None:
        if self.resources is not None:
            await self.resources.aclose()
        if self.engine is not None:
            await self.engine.dispose()
        if self.storage is not None:
            await self.storage.aclose()


def build_restore_services(settings: Settings, *, task: RestoreTask) -> RestoreServices:
    clock = SystemClock()
    if task is RestoreTask.EXPORT_DELETIONS:
        engine = create_engine(settings, application_name="plummy-maintenance")
        uow = SqlAlchemyUnitOfWorkFactory(create_session_factory(engine))
        return RestoreServices(export_deletions=ExportDeletions(uow, clock), engine=engine)
    if task is RestoreTask.OBJECTS:
        storage = create_storage(settings)
        return RestoreServices(
            export_objects=ExportObjects(storage), import_objects=ImportObjects(storage), storage=storage
        )
    resources = build_resources(settings)
    uow = SqlAlchemyUnitOfWorkFactory(resources.session_factory)
    clients = oauth_clients(settings, resources.http)
    return RestoreServices(
        reapply_deletions=ReapplyDeletions(uow, resources.jobs, clock, resources.secrets, clients),
        resources=resources,
    )


def build_evaluation_services(settings: Settings) -> EvaluationServices:
    resources = build_resources(settings)
    uow = SqlAlchemyUnitOfWorkFactory(resources.session_factory)
    clock = SystemClock()
    run = RunRetrievalEvaluation(
        uow, resources.knowledge.create, resources.documents.upload, resources.knowledge.retrieve, clock
    )
    return EvaluationServices(resources=resources, uow=uow, clock=clock, run=run)


@dataclass(frozen=True, slots=True)
class CliServices:
    """Server-side administration (``python -m plummy.cli``). Needs only PostgreSQL, not Redis."""

    engine: AsyncEngine
    setup_status: GetSetupStatus
    issue_bootstrap_token: IssueBootstrapToken
    create_administrator: CreateAdministrator
    reset_password: ResetPassword
    token_location: str
    secrets_status: GetSecretsStatus
    rotate_secrets: RotateSecrets | None

    async def aclose(self) -> None:
        await self.engine.dispose()


def build_cli_services(
    settings: Settings, *, password_hasher: PasswordHasher | None = None, clock: Clock | None = None
) -> CliServices:
    cipher = load_secret_cipher(settings)
    engine = create_engine(settings, application_name="plummy-cli")
    uow = SqlAlchemyUnitOfWorkFactory(create_session_factory(engine))
    clock = clock or SystemClock()
    hasher = password_hasher or Argon2PasswordHasher()
    store = FileBootstrapTokenStore(settings.bootstrap_dir)
    return CliServices(
        engine=engine,
        setup_status=GetSetupStatus(uow, web_setup_enabled=settings.setup_mode is SetupMode.WEB),
        issue_bootstrap_token=IssueBootstrapToken(uow, clock, store, ttl=settings.bootstrap_token_ttl),
        create_administrator=CreateAdministrator(uow, clock, hasher, store),
        reset_password=ResetPassword(uow, clock, hasher),
        token_location=store.location,
        secrets_status=GetSecretsStatus(uow, cipher),
        rotate_secrets=RotateSecrets(uow, cipher, clock) if cipher is not None else None,
    )
