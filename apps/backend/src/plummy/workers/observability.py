"""Task lifecycle logs: one structured event per state change (ARCHITECTURE.md §11, SECURITY_MODEL.md §14).

Logged: task id, task name, attempt number, duration, exception type. Never logged: task arguments, return
values and exception messages — later tasks carry document content and provider responses.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from celery import signals

logger = logging.getLogger("plummy.workers")

_started: dict[str, float] = {}
_lock = threading.Lock()


def _attempt(task: Any) -> int:
    request = getattr(task, "request", None)
    retries = getattr(request, "retries", 0) or 0
    return int(retries) + 1


def _on_prerun(task_id: str, task: Any, **_: Any) -> None:
    with _lock:
        _started[task_id] = time.monotonic()
    logger.info(
        "task started",
        extra={
            "event": "task.started",
            "task_id": task_id,
            "task_name": task.name,
            "attempt": _attempt(task),
        },
    )


def _on_postrun(task_id: str, task: Any, state: str | None = None, **_: Any) -> None:
    with _lock:
        started = _started.pop(task_id, None)
    duration_ms = round((time.monotonic() - started) * 1000) if started is not None else None
    level = logging.INFO if state == "SUCCESS" else logging.WARNING
    logger.log(
        level,
        "task finished",
        extra={
            "event": "task.finished",
            "task_id": task_id,
            "task_name": task.name,
            "attempt": _attempt(task),
            "state": state,
            "duration_ms": duration_ms,
        },
    )


def _on_retry(sender: Any = None, request: Any = None, reason: Any = None, **_: Any) -> None:
    logger.warning(
        "task retry scheduled",
        extra={
            "event": "task.retry",
            "task_id": getattr(request, "id", None),
            "task_name": getattr(sender, "name", None),
            "attempt": int(getattr(request, "retries", 0) or 0) + 1,
            "exc_type": type(reason).__name__ if isinstance(reason, BaseException) else None,
        },
    )


def _on_failure(sender: Any = None, task_id: str | None = None, exception: Any = None, **_: Any) -> None:
    logger.error(
        "task failed",
        extra={
            "event": "task.failed",
            "task_id": task_id,
            "task_name": getattr(sender, "name", None),
            "exc_type": type(exception).__name__,
        },
    )


def connect_task_logging() -> None:
    """Idempotent: ``dispatch_uid`` keeps one receiver per signal however often it is called."""
    signals.task_prerun.connect(_on_prerun, weak=False, dispatch_uid="plummy.task_prerun")
    signals.task_postrun.connect(_on_postrun, weak=False, dispatch_uid="plummy.task_postrun")
    signals.task_retry.connect(_on_retry, weak=False, dispatch_uid="plummy.task_retry")
    signals.task_failure.connect(_on_failure, weak=False, dispatch_uid="plummy.task_failure")
