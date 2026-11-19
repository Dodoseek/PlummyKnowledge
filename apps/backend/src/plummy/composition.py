"""Composition root: wires infrastructure adapters into application use cases (ARCHITECTURE.md §7).

Used by the API lifespan now and by Celery workers later, so both roles share the same wiring.
"""

from __future__ import annotations

import logging
import socket
from dataclasses import dataclass
from urllib.parse import urlsplit

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from plummy.application.auth.account import ChangePassword, ListMySessions, RevokeMySession
from plummy.application.auth.activation import ActivateAccount
from plummy.application.auth.sessions import SessionIssuer, SessionPolicy
from plummy.application.auth.throttle import AttemptThrottle
from plummy.application.auth.use_cases import AuthenticateSession, Login, Logout
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
from plummy.core.config import ConfigurationError, Settings, SetupMode
from plummy.core.instance_config import build_egress_policy, load_instance_config
from plummy.core.network.addresses import IPAddress
from plummy.core.network.policy import EgressPolicy
from plummy.core.network.urls import parse_ip
from plummy.infrastructure.clock import SystemClock
from plummy.infrastructure.db import create_engine, create_session_factory
from plummy.infrastructure.db.checks import DatabaseCheck, MigrationsCheck, SecretsCheck
from plummy.infrastructure.db.migrations import get_expected_heads
from plummy.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWorkFactory
from plummy.infrastructure.http.egress_audit import EgressDenialAuditor
from plummy.infrastructure.http.safe_client import SafeHttpClient
from plummy.infrastructure.integrations.ai.factory import HttpProviderGateway
from plummy.infrastructure.redis import create_redis_client
from plummy.infrastructure.redis.checks import RedisCheck
from plummy.infrastructure.redis.rate_limiter import RedisRateLimiter
from plummy.infrastructure.security.bootstrap_store import FileBootstrapTokenStore
from plummy.infrastructure.security.passwords import Argon2PasswordHasher
from plummy.infrastructure.security.secret_cipher import AesGcmSecretCipher, KeyRingError, load_keyring
from plummy.infrastructure.storage.local import LocalFileStorage, StorageCheck

logger = logging.getLogger(__name__)


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
    secrets: SecretVault
    http: SafeHttpClient
    storage: LocalFileStorage

    async def aclose(self) -> None:
        """Release connections. Each resource is closed even if another one fails to close."""
        try:
            await self.http.aclose()
        except Exception:
            logger.exception("failed to close outbound http client")
        try:
            await self.redis.aclose()
        except Exception:
            logger.exception("failed to close redis client")
        try:
            await self.engine.dispose()
        except Exception:
            logger.exception("failed to dispose database engine")


def create_storage(settings: Settings) -> LocalFileStorage:
    """Document storage (ARCHITECTURE.md §8.5); only the local backend exists before Phase 11."""
    return LocalFileStorage(settings.storage_path)


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


def load_egress_policy(settings: Settings) -> EgressPolicy:
    """The outbound network policy from CONFIG_FILE; an invalid file stops the process (ADR-007)."""
    config = load_instance_config(settings.config_file)
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
) -> Resources:
    """Create clients without connecting; connectivity is verified by readiness checks."""
    cipher = load_secret_cipher(settings)
    egress_policy = load_egress_policy(settings)
    expected_heads = get_expected_heads()
    engine = create_engine(settings)
    redis = create_redis_client(settings)
    storage = create_storage(settings)
    readiness = CheckReadiness(
        [
            DatabaseCheck(engine),
            MigrationsCheck(engine, expected_heads),
            RedisCheck(redis),
            SecretsCheck(engine, cipher.key_ids if cipher else frozenset()),
            StorageCheck(storage),
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
        secrets=vault,
        http=http,
        storage=storage,
    )


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
