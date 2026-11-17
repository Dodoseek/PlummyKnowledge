"""Readiness check for Redis (implements ``DependencyCheck``)."""

from __future__ import annotations

from redis.asyncio import Redis


class RedisUnavailableError(Exception):
    pass


class RedisCheck:
    name = "redis"

    def __init__(self, client: Redis) -> None:
        self._client = client

    async def check(self) -> None:
        if not await self._client.ping():
            raise RedisUnavailableError("PING did not return PONG")
