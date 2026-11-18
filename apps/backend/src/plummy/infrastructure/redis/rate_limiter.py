"""Fixed-window rate limiter on Redis (ADR-013 §4): atomic INCR + EXPIRE NX in one MULTI."""

from __future__ import annotations

from redis.asyncio import Redis
from redis.exceptions import RedisError

from plummy.application.ports import RateLimiterUnavailableError, RateLimitStatus

KEY_PREFIX = "plummy:rl:"


class RedisRateLimiter:
    def __init__(self, client: Redis, *, prefix: str = KEY_PREFIX) -> None:
        self._client = client
        self._prefix = prefix

    async def hit(self, key: str, *, limit: int, window_seconds: int) -> RateLimitStatus:
        full_key = self._prefix + key
        try:
            async with self._client.pipeline(transaction=True) as pipe:
                pipe.incr(full_key)
                pipe.expire(full_key, window_seconds, nx=True)
                pipe.ttl(full_key)
                count, _, ttl = await pipe.execute()
        except (RedisError, OSError) as exc:
            raise RateLimiterUnavailableError(type(exc).__name__) from None
        return self._status(int(count), limit, int(ttl), window_seconds, counted=True)

    async def peek(self, key: str, *, limit: int, window_seconds: int) -> RateLimitStatus:
        full_key = self._prefix + key
        try:
            async with self._client.pipeline(transaction=True) as pipe:
                pipe.get(full_key)
                pipe.ttl(full_key)
                raw, ttl = await pipe.execute()
        except (RedisError, OSError) as exc:
            raise RateLimiterUnavailableError(type(exc).__name__) from None
        return self._status(int(raw or 0), limit, int(ttl), window_seconds, counted=False)

    async def reset(self, key: str) -> None:
        try:
            await self._client.delete(self._prefix + key)
        except (RedisError, OSError) as exc:
            raise RateLimiterUnavailableError(type(exc).__name__) from None

    @staticmethod
    def _status(count: int, limit: int, ttl: int, window: int, *, counted: bool) -> RateLimitStatus:
        allowed = count <= limit if counted else count < limit
        retry_after = ttl if ttl > 0 else window
        return RateLimitStatus(allowed=allowed, retry_after_seconds=0 if allowed else retry_after)
