"""Async engine and session factory.

Session/transaction policy: one ``AsyncSession`` per request (or job), obtained through the API dependency
``get_db_session``. Transaction boundaries are explicit in use cases (``async with session.begin():``);
the dependency only guarantees rollback on error and closing the session. Nothing commits implicitly.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from plummy.core.config import Settings

CONNECT_TIMEOUT_SECONDS = 5
POOL_TIMEOUT_SECONDS = 5


def create_engine(settings: Settings, *, application_name: str = "plummy-api") -> AsyncEngine:
    """Create the engine. No connection is opened here; the pool connects lazily."""
    return create_async_engine(
        settings.database_url.get_secret_value(),
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=POOL_TIMEOUT_SECONDS,
        pool_pre_ping=True,
        connect_args={
            "timeout": CONNECT_TIMEOUT_SECONDS,
            "server_settings": {"application_name": application_name},
        },
    )


def create_session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
