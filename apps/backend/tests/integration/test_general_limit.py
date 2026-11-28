"""The ``general`` rate limit on real Redis (API_CONTRACTS §5, SECURITY_MODEL §13, Phase 12.5)."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from plummy.application.auth.general_limit import GeneralRateLimit
from plummy.application.errors import RateLimitedError
from plummy.application.ports import RateLimiterUnavailableError, RateLimitStatus
from tests.integration.harness import Harness, running_auth_app
from tests.integration.rbac import CSRF_HEADER, login, setup_admin_client

pytestmark = pytest.mark.integration

LIMIT = 10


@pytest.fixture
async def limited(
    migrated_database_url: str, redis_url: str, clean_rate_limits: None, tmp_path: Path
) -> AsyncIterator[Harness]:
    async with running_auth_app(
        migrated_database_url, redis_url, tmp_path / "bootstrap", rate_limit_general_per_minute=LIMIT
    ) as harness:
        yield harness


def assert_limited(response: httpx.Response) -> None:
    assert response.status_code == 429, response.text
    assert response.json()["code"] == "rate_limited"
    assert 1 <= int(response.headers["retry-after"]) <= 60


async def test_a_user_is_limited_per_minute_and_others_are_not(limited: Harness) -> None:
    admin = await setup_admin_client(limited)  # setup counts against the address, not the user
    for _ in range(LIMIT):
        assert (await admin.get("/api/v1/auth/me")).status_code == 200
    assert_limited(await admin.get("/api/v1/auth/me"))
    assert_limited(await admin.get("/api/v1/users"))  # every route, one counter per user
    await limited.create_user("other@example.com", "another long passphrase 42", "member")
    async with limited.new_client() as other:
        assert (await login(other, "other@example.com", "another long passphrase 42")).status_code == 200
        assert (await other.get("/api/v1/auth/me")).status_code == 200


async def test_requests_without_a_session_are_limited_per_address(limited: Harness) -> None:
    async with limited.new_client() as anonymous:
        for _ in range(LIMIT):
            assert (await anonymous.get("/api/v1/setup/status")).status_code == 200
        assert_limited(await anonymous.get("/api/v1/setup/status"))
        response = await anonymous.post(
            "/api/v1/auth/login", json={"email": "a@example.com", "password": "x" * 12}
        )
        assert_limited(response)
        # Health checks are never limited: orchestration keeps working.
        for _ in range(3):
            assert (await anonymous.get("/api/v1/health/live")).status_code == 200


async def test_the_chat_stream_is_not_counted(limited: Harness) -> None:
    admin = await setup_admin_client(limited)
    for _ in range(LIMIT + 3):
        response = await admin.post(
            "/api/v1/chat/stream",
            json={"assistant_id": str(uuid4()), "content": "Hi", "client_message_id": str(uuid4())},
        )
        # Refused by the chat itself (no such assistant), never by the general limit.
        assert response.status_code == 404, response.text
    assert (await admin.get("/api/v1/auth/me")).status_code == 200


async def test_a_refused_request_still_counts(limited: Harness) -> None:
    """Probing routes without the permission is limited like any other request."""
    await limited.create_user("member@example.com", "another long passphrase 42", "member")
    async with limited.new_client() as member:
        assert (await login(member, "member@example.com", "another long passphrase 42")).status_code == 200
        for _ in range(LIMIT):
            assert (await member.get("/api/v1/users")).status_code == 403
        assert_limited(await member.get("/api/v1/users"))
        assert CSRF_HEADER in member.headers


class DownLimiter:
    async def hit(self, key: str, *, limit: int, window_seconds: int) -> RateLimitStatus:
        raise RateLimiterUnavailableError("ConnectionError")

    async def peek(self, key: str, *, limit: int, window_seconds: int) -> RateLimitStatus:
        raise RateLimiterUnavailableError("ConnectionError")

    async def reset(self, key: str) -> None:
        raise RateLimiterUnavailableError("ConnectionError")


class CountingLimiter(DownLimiter):
    def __init__(self) -> None:
        self.keys: list[str] = []

    async def hit(self, key: str, *, limit: int, window_seconds: int) -> RateLimitStatus:
        self.keys.append(key)
        return RateLimitStatus(
            allowed=len(self.keys) <= limit, retry_after_seconds=0 if len(self.keys) <= limit else 42
        )


async def test_redis_down_lets_requests_through_with_one_warning(caplog: pytest.LogCaptureFixture) -> None:
    limit = GeneralRateLimit(DownLimiter(), per_minute=LIMIT)
    with caplog.at_level(logging.WARNING, logger="plummy.application.auth.general_limit"):
        for _ in range(5):
            await limit.for_user(uuid4())
            await limit.for_address("192.0.2.1")
    warnings = [record for record in caplog.records if "general rate limit" in record.getMessage()]
    assert len(warnings) == 1


async def test_keys_carry_no_personal_data_but_the_id_or_address() -> None:
    limiter = CountingLimiter()
    limit = GeneralRateLimit(limiter, per_minute=2)
    user_id = uuid4()
    await limit.for_user(user_id)
    await limit.for_address(None)
    assert limiter.keys == [f"general:user:{user_id}", "general:ip:unknown"]
    with pytest.raises(RateLimitedError) as exc_info:
        await limit.for_address(None)
    assert exc_info.value.retry_after_seconds == 42
