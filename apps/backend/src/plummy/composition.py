"""Composition root: wires infrastructure adapters into application use cases (ARCHITECTURE.md §7).

Used by the API lifespan now and by Celery workers later, so both roles share the same wiring.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from plummy.application.health import CheckReadiness
from plummy.core.config import Settings
from plummy.infrastructure.db import create_engine, create_session_factory
from plummy.infrastructure.db.checks import DatabaseCheck, MigrationsCheck
from plummy.infrastructure.db.migrations import get_expected_heads
from plummy.infrastructure.redis import create_redis_client
from plummy.infrastructure.redis.checks import RedisCheck

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Resources:
    """Process-wide resources owned by the application lifespan."""

    engine: AsyncEngine
    session_factory: async_sessionmaker[AsyncSession]
    redis: Redis
    readiness: CheckReadiness

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


def build_resources(settings: Settings) -> Resources:
    """Create clients without connecting; connectivity is verified by readiness checks."""
    expected_heads = get_expected_heads()
    engine = create_engine(settings)
    redis = create_redis_client(settings)
    readiness = CheckReadiness(
        [DatabaseCheck(engine), MigrationsCheck(engine, expected_heads), RedisCheck(redis)]
    )
    return Resources(
        engine=engine,
        session_factory=create_session_factory(engine),
        redis=redis,
        readiness=readiness,
    )
