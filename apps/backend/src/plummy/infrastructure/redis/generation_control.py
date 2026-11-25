"""Concurrent generations and stop requests on Redis (ADR-019 §2-3), shared by every API process.

The slots of a user are a sorted set of run IDs scored by their expiry: expired slots (a process that died
mid-answer) are dropped before counting, so a crash never holds a slot longer than its TTL. Taking a slot is
one Lua script, atomic against concurrent requests of the same user.
"""

from __future__ import annotations

import time
from typing import Final
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError

from plummy.application.ports import GenerationControlUnavailableError

KEY_PREFIX: Final = "plummy:chat:"

_ACQUIRE: Final = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', ARGV[1])
if redis.call('ZCARD', KEYS[1]) >= tonumber(ARGV[3]) then
  return 0
end
redis.call('ZADD', KEYS[1], ARGV[2], ARGV[4])
redis.call('PEXPIREAT', KEYS[1], ARGV[2])
return 1
"""


class RedisGenerationControl:
    def __init__(self, client: Redis, *, prefix: str = KEY_PREFIX) -> None:
        self._client = client
        self._prefix = prefix

    def _slots(self, user_id: UUID) -> str:
        return f"{self._prefix}active:{user_id.hex}"

    def _stop(self, run_id: UUID) -> str:
        return f"{self._prefix}stop:{run_id.hex}"

    async def acquire(self, user_id: UUID, run_id: UUID, *, limit: int, ttl_seconds: int) -> bool:
        now_ms = int(time.time() * 1000)
        expires_ms = now_ms + ttl_seconds * 1000
        try:
            taken = await self._client.eval(
                _ACQUIRE, 1, self._slots(user_id), now_ms, expires_ms, limit, run_id.hex
            )
        except (RedisError, OSError) as exc:
            raise GenerationControlUnavailableError(type(exc).__name__) from None
        return int(taken) == 1

    async def release(self, user_id: UUID, run_id: UUID) -> None:
        try:
            await self._client.zrem(self._slots(user_id), run_id.hex)
        except (RedisError, OSError) as exc:
            raise GenerationControlUnavailableError(type(exc).__name__) from None

    async def request_stop(self, run_id: UUID, *, ttl_seconds: int) -> None:
        try:
            await self._client.set(self._stop(run_id), "1", ex=ttl_seconds)
        except (RedisError, OSError) as exc:
            raise GenerationControlUnavailableError(type(exc).__name__) from None

    async def stop_requested(self, run_id: UUID) -> bool:
        try:
            return bool(await self._client.exists(self._stop(run_id)))
        except (RedisError, OSError) as exc:
            raise GenerationControlUnavailableError(type(exc).__name__) from None
