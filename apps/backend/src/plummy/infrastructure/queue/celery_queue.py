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
    EMBEDDINGS_QUEUE,
    INDEXING_TASK,
    INGESTION_QUEUE,
    INGESTION_TASK,
    MAINTENANCE_QUEUE,
    VECTOR_INDEX_TASK,
    close_celery_app,
    create_celery_app,
)


class CeleryJobQueue:
    def __init__(self, settings: Settings, *, namespace: str = DEFAULT_NAMESPACE) -> None:
        self._app: Any = create_celery_app(settings, namespace=namespace)

    async def enqueue_ingestion(self, job_id: UUID) -> str:
        return await self._send(INGESTION_TASK, job_id, INGESTION_QUEUE)

    async def enqueue_indexing(self, job_id: UUID) -> str:
        return await self._send(INDEXING_TASK, job_id, EMBEDDINGS_QUEUE)

    async def enqueue_vector_index(self, profile_id: UUID) -> str:
        return await self._send(VECTOR_INDEX_TASK, profile_id, MAINTENANCE_QUEUE)

    async def _send(self, task: str, object_id: UUID, queue: str) -> str:
        try:
            # ``send_task`` blocks on the broker connection; keep it off the event loop.
            result = await asyncio.to_thread(self._app.send_task, task, args=[str(object_id)], queue=queue)
        except (OperationalError, OSError) as exc:
            raise JobQueueUnavailableError(str(type(exc).__name__)) from None
        return str(result.id)

    def close(self) -> None:
        close_celery_app(self._app)
