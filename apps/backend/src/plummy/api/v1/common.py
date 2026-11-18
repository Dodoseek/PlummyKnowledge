"""Helpers shared by the API v1 routers."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from fastapi import Request, Response

from plummy.api.cookies import set_session_cookie
from plummy.application.auth.sessions import IssuedSession
from plummy.application.errors import InvalidQueryParameterError
from plummy.application.pagination import PageRequest, SortSpec, decode_cursor, parse_limit, parse_sort
from plummy.core.config import Settings


def no_store(response: Response) -> None:
    """Router dependency: responses carrying session state must never be cached."""
    response.headers["Cache-Control"] = "no-store"


def attach_session_cookie(response: Response, settings: Settings, issued: IssuedSession) -> None:
    set_session_cookie(
        response,
        settings,
        token=issued.token,
        expires_at=issued.context.session_expires_at,
        now=datetime.now(UTC),
    )


def allow_query(*names: str) -> Callable[[Request], None]:
    """Router-endpoint dependency: only allowlisted query parameters, each at most once (ADR-014)."""
    allowed = frozenset(names)

    def dependency(request: Request) -> None:
        seen: set[str] = set()
        for name, _ in request.query_params.multi_items():
            if name not in allowed:
                raise InvalidQueryParameterError(name, "unknown_query_parameter", "Unknown query parameter.")
            if name in seen:
                raise InvalidQueryParameterError(
                    name, "duplicate_query_parameter", "Repeated query parameter."
                )
            seen.add(name)

    return dependency


def page_request(
    sort: str | None, limit: int | None, cursor: str | None, allowed: Sequence[SortSpec], default: SortSpec
) -> PageRequest:
    spec = parse_sort(sort, allowed, default=default)
    return PageRequest(sort=spec, limit=parse_limit(limit), after=decode_cursor(cursor, spec))
