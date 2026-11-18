"""Attempt limits for login and setup (ADR-013 §4). Counters live in Redis; Redis down → fail closed."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass

from plummy.application.errors import RateLimitedError, ServiceUnavailableError
from plummy.application.ports import RateLimiter, RateLimiterUnavailableError

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Limit:
    name: str
    limit: int
    window_seconds: int


LOGIN_IP_EMAIL_PER_MINUTE = Limit("login:ip_email:1m", 5, 60)
LOGIN_IP_EMAIL_PER_HOUR = Limit("login:ip_email:1h", 20, 3600)
LOGIN_IP_PER_MINUTE = Limit("login:ip:1m", 30, 60)
LOGIN_FAILURES_PER_EMAIL = Limit("login:email_failures:15m", 10, 900)
SETUP_IP_PER_MINUTE = Limit("setup:ip:1m", 5, 60)

UNKNOWN_IP = "unknown"


def _email_key(email: str) -> str:
    # Keys never contain the email in clear text.
    return hashlib.sha256(email.encode("utf-8")).hexdigest()


class AttemptThrottle:
    def __init__(self, limiter: RateLimiter) -> None:
        self._limiter = limiter

    async def before_login(self, *, ip_address: str | None, email: str) -> None:
        ip = ip_address or UNKNOWN_IP
        email_key = _email_key(email)
        checks = [
            (LOGIN_IP_EMAIL_PER_MINUTE, f"{ip}|{email_key}", True),
            (LOGIN_IP_EMAIL_PER_HOUR, f"{ip}|{email_key}", True),
            (LOGIN_IP_PER_MINUTE, ip, True),
            (LOGIN_FAILURES_PER_EMAIL, email_key, False),
        ]
        await self._enforce(checks)

    async def login_failed(self, *, email: str) -> None:
        try:
            await self._limiter.hit(
                f"{LOGIN_FAILURES_PER_EMAIL.name}:{_email_key(email)}",
                limit=LOGIN_FAILURES_PER_EMAIL.limit,
                window_seconds=LOGIN_FAILURES_PER_EMAIL.window_seconds,
            )
        except RateLimiterUnavailableError:
            raise ServiceUnavailableError() from None

    async def login_succeeded(self, *, email: str) -> None:
        """Forget earlier failures. Best effort: the user is already authenticated."""
        try:
            await self._limiter.reset(f"{LOGIN_FAILURES_PER_EMAIL.name}:{_email_key(email)}")
        except RateLimiterUnavailableError:
            logger.warning("could not reset the login failure counter: rate limiter unavailable")

    async def before_setup(self, *, ip_address: str | None) -> None:
        await self._enforce([(SETUP_IP_PER_MINUTE, ip_address or UNKNOWN_IP, True)])

    async def _enforce(self, checks: list[tuple[Limit, str, bool]]) -> None:
        retry_after = 0
        try:
            for limit, subject, count in checks:
                key = f"{limit.name}:{subject}"
                if count:
                    status = await self._limiter.hit(
                        key, limit=limit.limit, window_seconds=limit.window_seconds
                    )
                else:
                    status = await self._limiter.peek(
                        key, limit=limit.limit, window_seconds=limit.window_seconds
                    )
                if not status.allowed:
                    retry_after = max(retry_after, status.retry_after_seconds)
        except RateLimiterUnavailableError:
            raise ServiceUnavailableError() from None
        if retry_after:
            logger.warning("attempt rate limit exceeded", extra={"limits": [c[0].name for c in checks]})
            raise RateLimitedError(retry_after)
