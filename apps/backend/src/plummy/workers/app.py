"""Celery application factory (ARCHITECTURE.md §3, §11).

The worker is an input adapter like the API: tasks call application use cases and hold no business rules.
Security-relevant settings:

- JSON only for messages and results (no pickle: a message from the broker must never execute code);
- every key in Redis has the ``plummy:celery:`` prefix, next to the rate-limit keys (``plummy:rl:``);
- results expire after a day: Redis is not a source of truth (DEPLOYMENT.md §5), durable job state lives in
  PostgreSQL tables of the features that need it (``ingestion_jobs``, ``sync_jobs``);
- ``acks_late`` + ``reject_on_worker_lost``: a task interrupted by a crash is redelivered, so every task
  must be idempotent (§11).
"""

from __future__ import annotations

from typing import Any

from celery import Celery

from plummy.core.config import Settings

DEFAULT_NAMESPACE = "plummy:celery:"
DEFAULT_QUEUE = "default"
# Dedicated queues (ARCHITECTURE.md §11): parsing runs on isolated workers, embeddings need egress.
INGESTION_QUEUE = "ingestion"
EMBEDDINGS_QUEUE = "embeddings"
MAINTENANCE_QUEUE = "maintenance"
CONNECTORS_QUEUE = "connectors"  # sync of external sources: network and secrets, the ``worker`` (ADR-020 §8)
INGESTION_TASK = "plummy.ingestion.run_job"  # stages 2-7, worker-ingestion (no network)
INDEXING_TASK = "plummy.ingestion.index_job"  # stages 8-12, worker with egress to embedding providers
VECTOR_INDEX_TASK = "plummy.maintenance.ensure_vector_index"
PURGE_TASK = "plummy.maintenance.purge_document"
SYNC_TASK = "plummy.connectors.sync"
# Periodic tasks, sent by the single ``scheduler`` (Celery beat) process.
CLEANUP_QUARANTINE_TASK = "plummy.maintenance.cleanup_quarantine"
REQUEUE_JOBS_TASK = "plummy.maintenance.requeue_jobs"
SWEEP_DELETED_TASK = "plummy.maintenance.sweep_deleted_documents"
SWEEP_CHAT_TASK = "plummy.maintenance.sweep_chat"
SCHEDULE_SYNCS_TASK = "plummy.maintenance.schedule_syncs"
PRUNE_VERSIONS_TASK = "plummy.maintenance.prune_document_versions"
MAINTENANCE_TASKS = (
    VECTOR_INDEX_TASK,
    PURGE_TASK,
    CLEANUP_QUARANTINE_TASK,
    REQUEUE_JOBS_TASK,
    SWEEP_DELETED_TASK,
    SWEEP_CHAT_TASK,
    SCHEDULE_SYNCS_TASK,
    PRUNE_VERSIONS_TASK,
)
# (task, interval in seconds). A run that is still waiting when the next one is due is dropped (``expires``).
BEAT_SCHEDULE = (
    (CLEANUP_QUARANTINE_TASK, 60 * 60),
    (REQUEUE_JOBS_TASK, 5 * 60),
    (SWEEP_DELETED_TASK, 10 * 60),
    (SWEEP_CHAT_TASK, 5 * 60),
    (SCHEDULE_SYNCS_TASK, 60),
    (PRUNE_VERSIONS_TASK, 60 * 60),
)
RESULT_TTL_SECONDS = 24 * 60 * 60
# Hard ceiling for any task; long-running tasks set their own limits explicitly.
DEFAULT_SOFT_TIME_LIMIT_SECONDS = 15 * 60
DEFAULT_TIME_LIMIT_SECONDS = DEFAULT_SOFT_TIME_LIMIT_SECONDS + 60
# Redelivery of unacknowledged messages; must exceed the longest task (acks_late).
VISIBILITY_TIMEOUT_SECONDS = 2 * 60 * 60

TASK_MODULES = (
    "plummy.workers.tasks.system",
    "plummy.workers.tasks.ingestion",
    "plummy.workers.tasks.maintenance",
    "plummy.workers.tasks.connectors",
)


def celery_config(
    settings: Settings, *, namespace: str = DEFAULT_NAMESPACE, queue: str = DEFAULT_QUEUE
) -> dict[str, Any]:
    redis_url = settings.redis_url.get_secret_value()
    return {
        "broker_url": redis_url,
        "result_backend": redis_url,
        "broker_transport_options": {
            "global_keyprefix": namespace,
            "visibility_timeout": VISIBILITY_TIMEOUT_SECONDS,
        },
        "result_backend_transport_options": {"global_keyprefix": namespace},
        "broker_connection_retry_on_startup": True,
        # Publishing fails fast when Redis is down instead of blocking the caller.
        "task_publish_retry_policy": {
            "max_retries": 3,
            "interval_start": 0,
            "interval_step": 0.5,
            "interval_max": 1,
        },
        "task_serializer": "json",
        "result_serializer": "json",
        "accept_content": ["json"],
        "result_accept_content": ["json"],
        "result_expires": RESULT_TTL_SECONDS,
        # One result backend (Redis client and pool) per app instead of one per thread: redis-py is
        # thread-safe, and the connections can be released on shutdown.
        "result_backend_thread_safe": True,
        "task_default_queue": queue,
        "task_routes": {
            INGESTION_TASK: {"queue": INGESTION_QUEUE},
            INDEXING_TASK: {"queue": EMBEDDINGS_QUEUE},
            SYNC_TASK: {"queue": CONNECTORS_QUEUE},
            **{task: {"queue": MAINTENANCE_QUEUE} for task in MAINTENANCE_TASKS},
        },
        "beat_schedule": {
            task: {"task": task, "schedule": float(interval), "options": {"expires": interval}}
            for task, interval in BEAT_SCHEDULE
        },
        "task_create_missing_queues": True,
        "task_track_started": True,
        "task_acks_late": True,
        "task_reject_on_worker_lost": True,
        "task_soft_time_limit": DEFAULT_SOFT_TIME_LIMIT_SECONDS,
        "task_time_limit": DEFAULT_TIME_LIMIT_SECONDS,
        "worker_prefetch_multiplier": 1,
        # Logging is configured by plummy (JSON to stdout); Celery must not replace it.
        "worker_hijack_root_logger": False,
        "worker_redirect_stdouts": False,
        "enable_utc": True,
        "timezone": "UTC",
    }


def create_celery_app(
    settings: Settings, *, namespace: str = DEFAULT_NAMESPACE, queue: str = DEFAULT_QUEUE
) -> Any:
    """Build a Celery app; ``namespace`` and ``queue`` are overridden only by tests for isolation."""
    app = Celery("plummy", set_as_current=False, include=list(TASK_MODULES))
    app.conf.update(celery_config(settings, namespace=namespace, queue=queue))
    return app


def close_celery_app(app: Any) -> None:
    """Release broker and result-backend connections (``Celery.close`` keeps the backend's Redis pool)."""
    try:
        client = getattr(getattr(app, "backend", None), "client", None)
        if client is not None:
            client.connection_pool.disconnect()
    finally:
        app.close()
