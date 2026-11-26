"""Structured JSON logging (ARCHITECTURE.md §11, SECURITY_MODEL.md §14).

Rules: one JSON object per line on stdout; ``request_id`` is attached automatically from the request
context; never log secrets, tokens, request bodies or document content.
"""

from __future__ import annotations

import json
import logging
import logging.config
import sys
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any

request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

# Attributes every LogRecord has; anything else was passed via ``extra=`` and is emitted as a field.
# ``color_message`` is uvicorn's ANSI-colored duplicate of the message.
_RESERVED_ATTRS = frozenset(
    vars(logging.LogRecord("", 0, "", 0, "", None, None)).keys()
    | {"message", "asctime", "taskName", "color_message"}
)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created, tz=UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = request_id_var.get()
        if request_id is not None:
            payload["request_id"] = request_id
        for key, value in record.__dict__.items():
            if key not in _RESERVED_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info and record.exc_info[0] is not None:
            payload["exc_type"] = record.exc_info[0].__name__
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


def configure_logging(level: str, *, quiet_loggers: tuple[str, ...] = ()) -> None:
    """Route the root logger and uvicorn loggers to a single JSON stdout handler.

    ``quiet_loggers`` are raised to ``CRITICAL``: third-party loggers that would write data we must not log
    (for example Celery's trace logger prints task return values and exception messages).
    """
    quiet = {name: {"level": "CRITICAL", "handlers": [], "propagate": True} for name in quiet_loggers}
    logging.config.dictConfig(
        {
            "version": 1,
            "disable_existing_loggers": False,
            "formatters": {"json": {"()": JsonFormatter}},
            "handlers": {
                "stdout": {"class": "logging.StreamHandler", "formatter": "json", "stream": sys.stdout},
            },
            "root": {"level": level, "handlers": ["stdout"]},
            "loggers": {
                "uvicorn": {"level": level, "handlers": [], "propagate": True},
                "uvicorn.error": {"level": level, "handlers": [], "propagate": True},
                # Access logs are written by our middleware (no query strings, with request_id).
                "uvicorn.access": {"level": "WARNING", "handlers": [], "propagate": False},
                # SQL echo would leak query parameters; keep SQLAlchemy quiet regardless of LOG_LEVEL.
                "sqlalchemy": {"level": "WARNING", "handlers": [], "propagate": True},
                # httpx logs every request URL at INFO (object keys, page tokens, file IDs): adapters log
                # their own redacted outcome instead.
                "httpx": {"level": "WARNING", "handlers": [], "propagate": True},
                "httpcore": {"level": "WARNING", "handlers": [], "propagate": True},
                **quiet,
            },
        }
    )
