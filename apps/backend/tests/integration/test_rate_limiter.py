"""Fixed-window limiter on the real Redis (ADR-013 §4)."""

from __future__ import annotations

import uuid

import pytest
from redis.asyncio import Redis

from plummy.application.ports import RateLimiterUnavailableError
from plummy.infrastructure.redis.rate_limiter import RedisRateLimiter

pytestmark = pytest.mark.integration


async def test_counts_within_window_and_reports_retry_after(redis_url: str) -> None:
    client = Redis.from_url(redis_url, decode_responses=True)
    limiter = RedisRateLimiter(client, prefix=f"plummy:test:{uuid.uuid4().hex}:")
    try:
        results = [await limiter.hit("k", limit=3, window_seconds=60) for _ in range(4)]
        assert [r.allowed for r in results] == [True, True, True, False]
        assert 0 < results[-1].retry_after_seconds <= 60
        assert not (await limiter.peek("k", limit=3, window_seconds=60)).allowed
        assert (await limiter.peek("other", limit=3, window_seconds=60)).allowed
        await limiter.reset("k")
        assert (await limiter.hit("k", limit=3, window_seconds=60)).allowed
        # The window is not extended by further hits (EXPIRE NX).
        ttl = await client.ttl(f"{limiter._prefix}k")
        assert 0 < ttl <= 60
    finally:
        await client.aclose()


async def test_unreachable_redis_raises_unavailable() -> None:
    client = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.5)
    limiter = RedisRateLimiter(client)
    try:
        with pytest.raises(RateLimiterUnavailableError):
            await limiter.hit("k", limit=1, window_seconds=60)
    finally:
        await client.aclose()
