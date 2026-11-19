"""Maintenance tasks (``maintenance`` queue). Idempotent: running one twice changes nothing.

The periodic ones are sent by the ``scheduler`` service (Celery beat, ``BEAT_SCHEDULE``); a run that misses
its slot expires instead of piling up behind an outage.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Coroutine
from typing import Any
from uuid import UUID

from celery import shared_task

from plummy.composition import MaintenanceServices, maintenance_runner, vector_index_runner
from plummy.core.config import get_settings
from plummy.workers.app import (
    CLEANUP_QUARANTINE_TASK,
    PURGE_TASK,
    REQUEUE_JOBS_TASK,
    SWEEP_DELETED_TASK,
    VECTOR_INDEX_TASK,
)


async def ensure_vector_index(profile_id: UUID) -> bool:
    async with vector_index_runner(get_settings()) as use_case:
        return await use_case.execute(profile_id)


async def _maintenance[T](run: Callable[[MaintenanceServices], Coroutine[Any, Any, T]]) -> T:
    async with maintenance_runner(get_settings()) as services:
        return await run(services)


def _retrying[T](task: Any, run: Callable[[], Coroutine[Any, Any, T]], *, countdown: int) -> T:
    try:
        return asyncio.run(run())
    except OSError as exc:  # database, broker or storage briefly unavailable
        raise task.retry(countdown=countdown, exc=exc) from None


def _ensure_vector_index(task: Any, profile_id: str) -> bool:
    """ADR-017 §2. The ID comes from the database (KB creation, the index phase), not from a request."""
    return _retrying(task, lambda: ensure_vector_index(UUID(profile_id)), countdown=60)


def _purge_document(task: Any, document_id: str) -> bool:
    """The ID comes from a committed deletion (the API or the sweep); the use case re-checks it."""
    target = UUID(document_id)
    return _retrying(task, lambda: _maintenance(lambda s: s.purge.execute(target)), countdown=60)


def _cleanup_quarantine(task: Any) -> int:
    return _retrying(task, lambda: _maintenance(lambda s: s.cleanup_quarantine.execute()), countdown=300)


def _requeue_jobs(task: Any) -> int:
    return _retrying(task, lambda: _maintenance(lambda s: s.requeue.execute()), countdown=60)


def _sweep_deleted(task: Any) -> int:
    return _retrying(task, lambda: _maintenance(lambda s: s.sweep_deleted.execute()), countdown=60)


run_ensure_vector_index = shared_task(
    name=VECTOR_INDEX_TASK, bind=True, max_retries=10, soft_time_limit=60 * 60, time_limit=61 * 60
)(_ensure_vector_index)
run_purge_document = shared_task(name=PURGE_TASK, bind=True, max_retries=10)(_purge_document)
run_cleanup_quarantine = shared_task(name=CLEANUP_QUARANTINE_TASK, bind=True, max_retries=2)(
    _cleanup_quarantine
)
run_requeue_jobs = shared_task(name=REQUEUE_JOBS_TASK, bind=True, max_retries=2)(_requeue_jobs)
run_sweep_deleted = shared_task(name=SWEEP_DELETED_TASK, bind=True, max_retries=2)(_sweep_deleted)
