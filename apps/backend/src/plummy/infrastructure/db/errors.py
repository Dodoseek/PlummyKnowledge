"""Exceptions that mean "the database is unreachable" (mapped to 503, not 500)."""

from __future__ import annotations

import socket

from sqlalchemy import exc as sa_exc

DATABASE_UNAVAILABLE_ERRORS: tuple[type[Exception], ...] = (
    sa_exc.OperationalError,
    sa_exc.InterfaceError,
    sa_exc.TimeoutError,  # connection pool exhausted
    ConnectionError,
    TimeoutError,
    socket.gaierror,
)
