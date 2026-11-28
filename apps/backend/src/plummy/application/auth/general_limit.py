"""The ``general`` rate limit: every API request, per user or, without a session, per IP (API_CONTRACTS §5).

A ceiling against floods and runaway clients, on top of the per-operation limits (login, upload, chat,
search…). Health checks and the chat stream, which has its own limit and slots, are not counted
(SECURITY_MODEL §13).

Redis down → the request is let through and a warning is logged at most once a minute: unlike the login and
setup limits, which protect secrets and fail closed, this one only bounds load, and closing it would take the
whole API down with Redis.
"""

from __future__ import annotations

import logging
import time
from uuid import UUID

from plummy.application.errors import RateLimitedError
from plummy.application.ports import RateLimiter, RateLimiterUnavailableError

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 60
WARNING_INTERVAL_SECONDS = 60.0
UNKNOWN_ADDRESS = "unknown"


class GeneralRateLimit:
    def __init__(self, limiter: RateLimiter, *, per_minute: int) -> None:
        self._limiter = limiter
        self._per_minute = per_minute
        self._warned_at = float("-inf")

    async def for_user(self, user_id: UUID) -> None:
        await self._hit(f"general:user:{user_id}")

    async def for_address(self, ip_address: str | None) -> None:
        await self._hit(f"general:ip:{ip_address or UNKNOWN_ADDRESS}")

    async def _hit(self, key: str) -> None:
        try:
            status = await self._limiter.hit(key, limit=self._per_minute, window_seconds=WINDOW_SECONDS)
        except RateLimiterUnavailableError:
            now = time.monotonic()
            if now - self._warned_at >= WARNING_INTERVAL_SECONDS:
                self._warned_at = now
                logger.warning("general rate limit unavailable: requests are not counted")
            return
        if not status.allowed:
            raise RateLimitedError(status.retry_after_seconds)
