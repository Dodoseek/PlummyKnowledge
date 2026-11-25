"""Pending OAuth authorizations on Redis (ADR-020 §1): one key per ``state`` hash, read and deleted at once
(``GETDEL``), so a ``state`` works once and expires after its TTL."""

from __future__ import annotations

import json
from typing import Final
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError

from plummy.application.sources.ports import OAuthStateUnavailableError, PendingAuthorization

KEY_PREFIX: Final = "plummy:oauth:state:"


class RedisOAuthStateStore:
    def __init__(self, client: Redis, *, prefix: str = KEY_PREFIX) -> None:
        self._client = client
        self._prefix = prefix

    async def put(self, key: str, pending: PendingAuthorization, *, ttl_seconds: int) -> None:
        value = json.dumps(
            {
                "data_source_id": str(pending.data_source_id),
                "user_id": str(pending.user_id),
                "session_id": str(pending.session_id),
                "code_verifier": pending.code_verifier,
            }
        )
        try:
            await self._client.set(self._prefix + key, value, ex=ttl_seconds, nx=True)
        except (RedisError, OSError) as exc:
            raise OAuthStateUnavailableError(type(exc).__name__) from None

    async def take(self, key: str) -> PendingAuthorization | None:
        try:
            raw = await self._client.getdel(self._prefix + key)
        except (RedisError, OSError) as exc:
            raise OAuthStateUnavailableError(type(exc).__name__) from None
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            return PendingAuthorization(
                data_source_id=UUID(data["data_source_id"]),
                user_id=UUID(data["user_id"]),
                session_id=UUID(data["session_id"]),
                code_verifier=str(data["code_verifier"]),
            )
        except (ValueError, KeyError, TypeError):
            return None
