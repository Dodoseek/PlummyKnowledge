"""Generation slots and stop flags on the real Redis (ADR-019 §2-3)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis

from plummy.application.ports import GenerationControlUnavailableError
from plummy.infrastructure.redis.generation_control import RedisGenerationControl

pytestmark = pytest.mark.integration


@pytest.fixture
async def control(redis_url: str) -> AsyncIterator[RedisGenerationControl]:
    client = Redis.from_url(redis_url, decode_responses=True)
    prefix = f"plummy:test:{uuid.uuid4().hex}:"
    try:
        yield RedisGenerationControl(client, prefix=prefix)
    finally:
        keys = [key async for key in client.scan_iter(match=f"{prefix}*")]
        if keys:
            await client.delete(*keys)
        await client.aclose()


async def test_slots_are_per_user_and_released(control: RedisGenerationControl) -> None:
    user, other = uuid.uuid4(), uuid.uuid4()
    runs = [uuid.uuid4() for _ in range(3)]
    taken = [await control.acquire(user, run, limit=2, ttl_seconds=60) for run in runs]
    assert taken == [True, True, False]
    assert await control.acquire(other, uuid.uuid4(), limit=2, ttl_seconds=60)
    await control.release(user, runs[0])
    await control.release(user, runs[0])  # releasing twice is harmless
    assert await control.acquire(user, runs[2], limit=2, ttl_seconds=60)


async def test_concurrent_requests_never_exceed_the_limit(control: RedisGenerationControl) -> None:
    user = uuid.uuid4()
    results = await asyncio.gather(
        *(control.acquire(user, uuid.uuid4(), limit=2, ttl_seconds=60) for _ in range(20))
    )
    assert results.count(True) == 2


async def test_a_slot_expires_after_its_ttl(control: RedisGenerationControl) -> None:
    user = uuid.uuid4()
    assert await control.acquire(user, uuid.uuid4(), limit=1, ttl_seconds=1)
    assert not await control.acquire(user, uuid.uuid4(), limit=1, ttl_seconds=1)
    await asyncio.sleep(1.1)
    assert await control.acquire(user, uuid.uuid4(), limit=1, ttl_seconds=1)


async def test_stop_flag(control: RedisGenerationControl) -> None:
    run = uuid.uuid4()
    assert not await control.stop_requested(run)
    await control.request_stop(run, ttl_seconds=60)
    await control.request_stop(run, ttl_seconds=60)
    assert await control.stop_requested(run)
    assert not await control.stop_requested(uuid.uuid4())


async def test_unreachable_redis_raises_unavailable() -> None:
    client = Redis.from_url("redis://127.0.0.1:1/0", socket_connect_timeout=0.5)
    control = RedisGenerationControl(client)
    try:
        with pytest.raises(GenerationControlUnavailableError):
            await control.acquire(uuid.uuid4(), uuid.uuid4(), limit=2, ttl_seconds=60)
        with pytest.raises(GenerationControlUnavailableError):
            await control.request_stop(uuid.uuid4(), ttl_seconds=60)
        with pytest.raises(GenerationControlUnavailableError):
            await control.stop_requested(uuid.uuid4())
    finally:
        await client.aclose()
