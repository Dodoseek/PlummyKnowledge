"""Composition root: wires infrastructure adapters into application use cases (ARCHITECTURE.md §7).

Used by the API lifespan now and by Celery workers later, so both roles share the same wiring.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from plummy.application.auth.sessions import SessionIssuer, SessionPolicy
from plummy.application.auth.throttle import AttemptThrottle
from plummy.application.auth.use_cases import AuthenticateSession, Login, Logout
from plummy.application.health import CheckReadiness
from plummy.application.ports import Clock, PasswordHasher
from plummy.application.setup.use_cases import (
    CreateAdministrator,
    GetSetupStatus,
    InitializeInstance,
    IssueBootstrapToken,
    ResetPassword,
)
from plummy.core.config import Settings, SetupMode
from plummy.infrastructure.clock import SystemClock
from plummy.infrastructure.db import create_engine, create_session_factory
from plummy.infrastructure.db.checks import DatabaseCheck, MigrationsCheck
from plummy.infrastructure.db.migrations import get_expected_heads
from plummy.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWorkFactory
from plummy.infrastructure.redis import create_redis_client
from plummy.infrastructure.redis.checks import RedisCheck
from plummy.infrastructure.redis.rate_limiter import RedisRateLimiter
from plummy.infrastructure.security.bootstrap_store import FileBootstrapTokenStore
from plummy.infrastructure.security.passwords import Argon2PasswordHasher

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class AuthServices:
    """Setup and authentication use cases used by the API."""

    authenticate: AuthenticateSession
    login: Login
    logout: Logout
    setup_status: GetSetupStatus
    initialize: InitializeInstance


@dataclass(frozen=True, slots=True)
class Resources:
    """Process-wide resources owned by the application lifespan."""

    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    redis: Redis
    readiness: CheckReadiness
    auth: AuthServices

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


def build_resources(
    settings: Settings,
    *,
    password_hasher: PasswordHasher | None = None,
    clock: Clock | None = None,
) -> Resources:
    """Create clients without connecting; connectivity is verified by readiness checks."""
    expected_heads = get_expected_heads()
    engine = create_engine(settings)
    redis = create_redis_client(settings)
    readiness = CheckReadiness(
        [DatabaseCheck(engine), MigrationsCheck(engine, expected_heads), RedisCheck(redis)]
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
    )
    return Resources(
        engine=engine,
        session_factory=session_factory,
        redis=redis,
        readiness=readiness,
        auth=auth,
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

    async def aclose(self) -> None:
        await self.engine.dispose()


def build_cli_services(
    settings: Settings, *, password_hasher: PasswordHasher | None = None, clock: Clock | None = None
) -> CliServices:
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
    )
