"""ASGI application factory.

Run with ``uvicorn --factory plummy.main:create_app``: nothing is created or connected at import time.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncEngine

from plummy.api.errors import REQUEST_ID_HEADER, register_exception_handlers
from plummy.api.middleware import RequestContextMiddleware
from plummy.api.router import api_router
from plummy.composition import (
    Resources,
    build_resources,
    check_storage_configuration,
    load_egress_policy,
    load_secret_cipher,
)
from plummy.core.config import ConfigurationError, ProcessRole, Settings, get_settings
from plummy.core.logging import configure_logging
from plummy.core.version import get_version
from plummy.infrastructure.db.engine import create_engine
from plummy.infrastructure.db.errors import DATABASE_UNAVAILABLE_ERRORS
from plummy.infrastructure.db.roles import inspect_role

logger = logging.getLogger("plummy")

ResourcesFactory = Callable[[Settings], Resources]


def report_configuration_error(exc: ConfigurationError) -> None:
    """Log a configuration error as JSON. The message never contains secret values."""
    configure_logging("INFO")
    logger.critical("startup aborted: %s", exc)


def _load_settings_or_exit() -> Settings:
    try:
        return get_settings()
    except ConfigurationError as exc:
        report_configuration_error(exc)
        raise SystemExit(1) from None


SEPARATED_ROLE_REQUIRED = (
    "the database role of this process can change or delete audit records (schema owner, superuser or "
    "UPDATE/DELETE on audit_logs): the services must connect with the runtime role "
    "(ADR-024, DEPLOYMENT.md §6.1)"
)


async def check_database_role(settings: Settings, engine: AsyncEngine) -> None:
    """ADR-024 §4: a service must not run with a role that can rewrite the audit log.

    Production refuses to start; elsewhere a warning. ``migrate`` is the owner by design. A database that is
    not reachable or not migrated yet is left to readiness.
    """
    if settings.process_role is ProcessRole.MIGRATE:
        return
    try:
        status = await inspect_role(engine)
    except DATABASE_UNAVAILABLE_ERRORS:
        logger.warning("database role not checked: database unavailable")
        return
    if status is None or status.separated:
        return
    if settings.is_production:
        raise ConfigurationError(SEPARATED_ROLE_REQUIRED)
    logger.warning("%s (allowed outside production)", SEPARATED_ROLE_REQUIRED)


async def _check_database_role_once(settings: Settings) -> None:
    engine = create_engine(settings, application_name=f"plummy-{settings.process_role.value}-check")
    try:
        await check_database_role(settings, engine)
    finally:
        await engine.dispose()


def check_configuration() -> int:
    """Validate configuration without starting the server: 0 if valid, 1 otherwise.

    Run as ``python -m plummy.check_config`` before ``uvicorn --reload``: the reloader process keeps running
    when the application fails to import, so a fatal configuration error would not stop the container.
    """
    try:
        settings = get_settings()
        load_secret_cipher(settings)
        load_egress_policy(settings)
        check_storage_configuration(settings)
        configure_logging(settings.log_level.value)
        asyncio.run(_check_database_role_once(settings))
    except ConfigurationError as exc:
        report_configuration_error(exc)
        return 1
    return 0


def create_app(
    settings: Settings | None = None,
    *,
    resources_factory: ResourcesFactory = build_resources,
) -> FastAPI:
    settings = settings if settings is not None else _load_settings_or_exit()
    configure_logging(settings.log_level.value)
    version = get_version()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resources = resources_factory(settings)
        app.state.resources = resources
        try:
            await check_database_role(settings, resources.engine)
            # Informational only: the process stays up and /health/ready reports 503 until
            # dependencies recover, so orchestration can tell "starting" from "broken".
            report = await resources.readiness.execute()
            logger.info(
                "application started",
                extra={
                    "version": version,
                    "settings": settings.safe_summary(),
                    "dependencies": {name: str(check) for name, check in report.checks.items()},
                },
            )
            if settings.secret_key_generated:
                logger.warning("SECRET_KEY is not set: using an ephemeral key (development/test only)")
            yield
        finally:
            await resources.aclose()
            logger.info("application stopped")

    app = FastAPI(
        title="PlummyKnowledge API",
        version=version,
        debug=settings.app_debug,
        lifespan=lifespan,
        openapi_url="/api/v1/openapi.json",
        # Swagger UI pulls JS/CSS from a public CDN: off in production unless API_DOCS_ENABLED=true.
        docs_url="/api/v1/docs" if settings.docs_enabled else None,
        redoc_url=None,
    )
    app.state.settings = settings
    register_exception_handlers(app)

    if settings.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_origins,
            allow_credentials=True,
            allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
            allow_headers=["Content-Type", "X-CSRF-Token", "X-Request-ID", "Idempotency-Key"],
            expose_headers=[REQUEST_ID_HEADER],
        )
    # Added last → outermost: every response (including CORS preflight) gets a request ID.
    app.add_middleware(RequestContextMiddleware)

    app.include_router(api_router)
    return app
