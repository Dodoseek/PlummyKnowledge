"""Maintenance tasks (``maintenance`` queue). Idempotent: running one twice changes nothing."""

from __future__ import annotations

import asyncio
from typing import Any
from uuid import UUID

from celery import shared_task

from plummy.composition import vector_index_runner
from plummy.core.config import get_settings
from plummy.workers.app import VECTOR_INDEX_TASK


async def ensure_vector_index(profile_id: UUID) -> bool:
    async with vector_index_runner(get_settings()) as use_case:
        return await use_case.execute(profile_id)


def _ensure_vector_index(task: Any, profile_id: str) -> bool:
    """ADR-017 §2. The ID comes from the database (KB creation, the index phase), not from a request."""
    try:
        return asyncio.run(ensure_vector_index(UUID(profile_id)))
    except OSError as exc:
        raise task.retry(countdown=60, exc=exc) from None


run_ensure_vector_index = shared_task(
    name=VECTOR_INDEX_TASK, bind=True, max_retries=10, soft_time_limit=60 * 60, time_limit=61 * 60
)(_ensure_vector_index)
