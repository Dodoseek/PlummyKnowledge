"""Composition root: wires infrastructure adapters into application use cases (ARCHITECTURE.md §7).

Used by the API lifespan now and by Celery workers later, so both roles share the same wiring.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

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
from plummy.infrastructure.clock import SystemClock
from plummy.infrastructure.db import create_engine, create_session_factory
from plummy.infrastructure.db.checks import DatabaseCheck, MigrationsCheck, SecretsCheck
from plummy.infrastructure.db.migrations import get_expected_heads
from plummy.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWorkFactory
from plummy.infrastructure.redis import create_redis_client
from plummy.infrastructure.redis.checks import RedisCheck
from plummy.infrastructure.redis.rate_limiter import RedisRateLimiter
from plummy.infrastructure.security.bootstrap_store import FileBootstrapTokenStore
from plummy.infrastructure.security.passwords import Argon2PasswordHasher
from plummy.infrastructure.security.secret_cipher import AesGcmSecretCipher, KeyRingError, load_keyring

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
class AdminServices:
    """Administration use cases; each one checks the caller's permissions itself."""

    users: UserServices
    groups: GroupServices
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

    async def aclose(self) -> None:
        """Release connections. Each resource is closed even if another one fails to close."""
        try:
            await self.redis.aclose()
        except Exception:
            logger.exception("failed to close redis client")
        try:
            await self.engine.dispose()
        except Exception:
            logger.exception("failed to dispose database engine")


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


def instance_settings_view(settings: Settings, cipher: SecretCipher | None) -> InstanceSettingsView:
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
    )


def build_resources(
    settings: Settings,
    *,
    password_hasher: PasswordHasher | None = None,
    clock: Clock | None = None,
) -> Resources:
    """Create clients without connecting; connectivity is verified by readiness checks."""
    cipher = load_secret_cipher(settings)
    expected_heads = get_expected_heads()
    engine = create_engine(settings)
    redis = create_redis_client(settings)
    readiness = CheckReadiness(
        [
            DatabaseCheck(engine),
            MigrationsCheck(engine, expected_heads),
            RedisCheck(redis),
            SecretsCheck(engine, cipher.key_ids if cipher else frozenset()),
        ]
    )
    session_factory = create_session_factory(engine)
    uow = SqlAlchemyUnitOfWorkFactory(session_factory)
    clock = clock or SystemClock()
    hasher = password_hasher or Argon2PasswordHasher()
    throttle = AttemptThrottle(RedisRateLimiter(redis))
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
        roles=ListRoles(uow),
        permissions=ListPermissions(uow),
        instance_settings=GetInstanceSettings(instance_settings_view(settings, cipher)),
    )
    return Resources(
        engine=engine,
        session_factory=session_factory,
        redis=redis,
        readiness=readiness,
        auth=auth,
        admin=admin,
        secrets=SecretVault(cipher, clock),
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
