"""Uniform error responses: RFC 9457 problem details (ADR-010, API_CONTRACTS.md §1.5).

Every error has ``code`` (stable, machine-readable) and ``correlation_id`` (= ``X-Request-ID``).
Responses never include stack traces, SQL, secrets or submitted values; 5xx responses carry no ``detail``.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from http import HTTPStatus
from typing import Any, cast

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from plummy.api.cookies import clear_session_cookie
from plummy.application.errors import (
    ApplicationError,
    AuthenticationRequiredError,
    ConflictError,
    InvalidCredentialsError,
    InvalidInputError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitedError,
    ServiceUnavailableError,
    SetupAlreadyCompletedError,
    SetupTokenInvalidError,
)
from plummy.infrastructure.db.errors import DATABASE_UNAVAILABLE_ERRORS

logger = logging.getLogger(__name__)

PROBLEM_JSON = "application/problem+json"


class CsrfFailedError(Exception):
    """Missing or wrong ``Origin``/``Referer`` or ``X-CSRF-Token`` (ADR-013 §2)."""


REQUEST_ID_HEADER = "X-Request-ID"

_CODES_BY_STATUS: dict[int, str] = {
    400: "bad_request",
    401: "unauthenticated",
    403: "permission_denied",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "validation_error",
    429: "rate_limited",
    503: "service_unavailable",
}


def _request_id(request: Request) -> str | None:
    value = getattr(request.state, "request_id", None)
    return value if isinstance(value, str) else None


def problem_response(
    request: Request,
    *,
    status: int,
    code: str,
    detail: str | None = None,
    errors: list[dict[str, str]] | None = None,
    headers: Mapping[str, str] | None = None,
) -> JSONResponse:
    request_id = _request_id(request)
    body: dict[str, Any] = {
        "type": "about:blank",
        "title": HTTPStatus(status).phrase,
        "status": status,
        "code": code,
        "correlation_id": request_id,
    }
    if detail is not None:
        body["detail"] = detail
    if errors is not None:
        body["errors"] = errors
    response_headers = dict(headers or {})
    # Error responses are never cacheable (some carry authentication state).
    response_headers["Cache-Control"] = "no-store"
    if request_id is not None:
        response_headers[REQUEST_ID_HEADER] = request_id
    return JSONResponse(body, status_code=status, headers=response_headers, media_type=PROBLEM_JSON)


async def http_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    http_exc = cast(StarletteHTTPException, exc)
    status = http_exc.status_code
    code = _CODES_BY_STATUS.get(status, "internal_error" if status >= 500 else "bad_request")
    # Starlette defaults ``detail`` to the status phrase; only pass through a custom client-error detail.
    detail = http_exc.detail if status < 500 and http_exc.detail != HTTPStatus(status).phrase else None
    return problem_response(request, status=status, code=code, detail=detail, headers=http_exc.headers)


async def validation_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    validation_exc = cast(RequestValidationError, exc)
    errors = [
        {
            "field": ".".join(str(part) for part in error.get("loc", ())),
            "code": str(error.get("type", "invalid")),
            "message": str(error.get("msg", "Invalid value")),
        }
        for error in validation_exc.errors()
    ]
    return problem_response(
        request,
        status=422,
        code="validation_error",
        detail="Request validation failed.",
        errors=errors,
    )


async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error(
        "unhandled exception",
        exc_info=exc,
        extra={"request_id": _request_id(request), "method": request.method, "path": request.url.path},
    )
    return problem_response(request, status=500, code="internal_error")


# Most specific first: the first matching class wins.
_APPLICATION_STATUS: tuple[tuple[type[ApplicationError], int], ...] = (
    (AuthenticationRequiredError, 401),
    (InvalidCredentialsError, 401),
    (PermissionDeniedError, 403),
    (SetupTokenInvalidError, 403),
    (NotFoundError, 404),
    (SetupAlreadyCompletedError, 409),
    (ConflictError, 409),
    (InvalidInputError, 422),
    (RateLimitedError, 429),
    (ServiceUnavailableError, 503),
)


def _status_for(exc: ApplicationError) -> int:
    for error_type, status in _APPLICATION_STATUS:
        if isinstance(exc, error_type):
            return status
    return 400


async def application_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    app_exc = cast(ApplicationError, exc)
    status = _status_for(app_exc)
    headers: dict[str, str] = {}
    if isinstance(app_exc, RateLimitedError):
        headers["Retry-After"] = str(app_exc.retry_after_seconds)
    if isinstance(app_exc, InvalidInputError):
        response = problem_response(
            request,
            status=422,
            code=app_exc.code,
            detail="Request validation failed.",
            errors=[
                {
                    "field": f"{app_exc.location}.{app_exc.field}",
                    "code": app_exc.error_code,
                    "message": str(app_exc),
                }
            ],
        )
    else:
        detail = app_exc.message if status < 500 else None
        response = problem_response(request, status=status, code=app_exc.code, detail=detail, headers=headers)
    if isinstance(app_exc, AuthenticationRequiredError):
        # An invalid session cookie is useless: tell the browser to drop it (ADR-013).
        clear_session_cookie(response, request.app.state.settings)
    return response


async def csrf_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.warning("csrf check failed", extra={"method": request.method, "path": request.url.path})
    return problem_response(
        request, status=403, code="csrf_failed", detail="The request origin or CSRF token is not valid."
    )


async def database_unavailable_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.error("database unavailable", extra={"error_type": type(exc).__name__})
    return problem_response(request, status=503, code="service_unavailable")


def register_exception_handlers(app: FastAPI) -> None:
    app.add_exception_handler(StarletteHTTPException, http_exception_handler)
    app.add_exception_handler(RequestValidationError, validation_exception_handler)
    app.add_exception_handler(ApplicationError, application_exception_handler)
    app.add_exception_handler(CsrfFailedError, csrf_exception_handler)
    for error_type in DATABASE_UNAVAILABLE_ERRORS:
        app.add_exception_handler(error_type, database_unavailable_handler)
    app.add_exception_handler(Exception, unhandled_exception_handler)
