"""``JobQueue`` on Celery (Redis broker). The message body is the job ID only (ADR-008)."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from kombu.exceptions import OperationalError

from plummy.application.ports import JobQueueUnavailableError
from plummy.core.config import Settings
from plummy.workers.app import (
    DEFAULT_NAMESPACE,
    INGESTION_QUEUE,
    INGESTION_TASK,
    close_celery_app,
    create_celery_app,
)


class CeleryJobQueue:
    def __init__(self, settings: Settings, *, namespace: str = DEFAULT_NAMESPACE) -> None:
        self._app: Any = create_celery_app(settings, namespace=namespace)

    async def enqueue_ingestion(self, job_id: UUID) -> str:
        try:
            # ``send_task`` blocks on the broker connection; keep it off the event loop.
            result = await asyncio.to_thread(
                self._app.send_task, INGESTION_TASK, args=[str(job_id)], queue=INGESTION_QUEUE
            )
        except (OperationalError, OSError) as exc:
            raise JobQueueUnavailableError(str(type(exc).__name__)) from None
        return str(result.id)

    def close(self) -> None:
        close_celery_app(self._app)
