"""Centralised Redis connection (one pooled client per process)."""

from __future__ import annotations

from redis.asyncio import Redis

from plummy.core.config import Settings

SOCKET_TIMEOUT_SECONDS = 2.0


def create_redis_client(settings: Settings) -> Redis:
    """Create the client. The pool connects lazily; close with ``await client.aclose()``."""
    return Redis.from_url(
        settings.redis_url.get_secret_value(),
        socket_connect_timeout=SOCKET_TIMEOUT_SECONDS,
        socket_timeout=SOCKET_TIMEOUT_SECONDS,
        health_check_interval=30,
        decode_responses=True,
    )
