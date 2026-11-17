"""Request context middleware: correlation ID and access log (API_CONTRACTS.md §1.8, ARCHITECTURE.md §11).

Implemented as pure ASGI (not ``BaseHTTPMiddleware``) so it does not buffer streaming/SSE responses.
"""

from __future__ import annotations

import logging
import re
import time
import uuid

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from plummy.core.logging import request_id_var

logger = logging.getLogger("plummy.access")

REQUEST_ID_HEADER = "x-request-id"
# Accept client-provided IDs only in a safe shape; anything else is replaced (prevents log injection).
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


def _incoming_request_id(scope: Scope) -> str | None:
    for name, value in scope.get("headers", ()):
        if name == b"x-request-id":
            try:
                candidate = value.decode("ascii")
            except UnicodeDecodeError:
                return None
            return candidate if _REQUEST_ID_RE.fullmatch(candidate) else None
    return None


class RequestContextMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = _incoming_request_id(scope) or uuid.uuid4().hex
        # ``scope["state"]`` backs ``request.state``; error handlers outside this middleware read it there.
        scope.setdefault("state", {})["request_id"] = request_id
        token = request_id_var.set(request_id)
        status_code = 500
        started = time.perf_counter()

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            # Path only: query strings may carry tokens (e.g. OAuth callbacks) and are never logged.
            logger.info(
                "request completed",
                extra={
                    "method": scope.get("method"),
                    "path": scope.get("path"),
                    "status": status_code,
                    "duration_ms": round((time.perf_counter() - started) * 1000, 2),
                },
            )
            request_id_var.reset(token)
