"""Celery worker against the real Redis broker: delivery, retries, failure, JSON-only, namespacing.

Each test gets its own key prefix and queue, so a dev ``worker`` container on the same Redis never picks up
the test's messages.
"""

from __future__ import annotations

import gc
import io
import json
import logging
import uuid
import warnings
from collections.abc import Iterator
from typing import Any

import pytest
from celery.contrib.testing.worker import start_worker
from celery.exceptions import TimeoutError as ResultTimeoutError
from redis import Redis

from plummy.cli.app import main
from plummy.core.config import Settings, get_settings
from plummy.workers.app import close_celery_app, create_celery_app
from plummy.workers.observability import connect_task_logging
from plummy.workers.tasks.system import PING_TASK

pytestmark = [pytest.mark.integration, pytest.mark.usefixtures("redis_client", "collect_celery_sockets")]


@pytest.fixture
def collect_celery_sockets() -> Iterator[None]:
    """The in-process test worker leaks a Redis socket inside Celery's own retry/result path; collect it here
    so the ResourceWarning does not surface later in an unrelated test. Only ResourceWarning is silenced, and
    only during this collection."""
    yield
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ResourceWarning)
        gc.collect()


@pytest.fixture
def settings(redis_url: str) -> Iterator[Settings]:
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.fixture
def namespace() -> str:
    return f"plummy:test:{uuid.uuid4().hex[:12]}:"


@pytest.fixture
def queue(namespace: str) -> str:
    return f"test-{namespace.split(':')[2]}"


@pytest.fixture
def redis_client(redis_url: str, namespace: str) -> Iterator[Redis]:
    client = Redis.from_url(redis_url, decode_responses=True)
    try:
        yield client
    finally:
        keys = list(client.scan_iter(match=f"{namespace}*"))
        if keys:
            client.delete(*keys)
        client.close()


@pytest.fixture
def celery_app(settings: Settings, namespace: str, queue: str) -> Iterator[Any]:
    app = create_celery_app(settings, namespace=namespace, queue=queue)
    app.loader.import_default_modules()
    connect_task_logging()
    try:
        yield app
    finally:
        close_celery_app(app)


@pytest.fixture
def worker(celery_app: Any) -> Iterator[None]:
    with start_worker(celery_app, pool="solo", concurrency=1, perform_ping_check=False, loglevel="WARNING"):
        yield


@pytest.mark.usefixtures("worker")
def test_ping_is_delivered_and_completed(celery_app: Any, redis_client: Redis, namespace: str) -> None:
    result = celery_app.send_task(PING_TASK)
    assert result.get(timeout=15) == {"status": "ok", "attempts": 1}
    assert result.state == "SUCCESS"

    # The result lives under the plummy namespace, as JSON, with an expiry.
    key = f"{namespace}celery-task-meta-{result.id}"
    stored = redis_client.get(key)
    assert stored is not None
    assert json.loads(stored)["status"] == "SUCCESS"
    assert 0 < redis_client.ttl(key) <= 24 * 60 * 60
    assert redis_client.get(f"celery-task-meta-{result.id}") is None


@pytest.mark.usefixtures("worker")
def test_transient_failures_are_retried_until_success(
    celery_app: Any, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="plummy.workers")
    result = celery_app.send_task(PING_TASK, kwargs={"simulate_failures": 2})
    assert result.get(timeout=30) == {"status": "ok", "attempts": 3}

    events = [
        (record.__dict__.get("event"), record.__dict__.get("attempt"))
        for record in caplog.records
        if record.name == "plummy.workers" and record.__dict__.get("task_id") == result.id
    ]
    assert events.count(("task.retry", 1)) == 1
    assert events.count(("task.retry", 2)) == 1
    assert ("task.started", 3) in events
    assert "simulate_failures" not in caplog.text


@pytest.mark.usefixtures("worker")
def test_invalid_input_fails_without_retry(celery_app: Any, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="plummy.workers")
    result = celery_app.send_task(PING_TASK, kwargs={"simulate_failures": 99})
    with pytest.raises(ValueError, match="between"):
        result.get(timeout=15)
    assert result.state == "FAILURE"
    retries = [r for r in caplog.records if r.__dict__.get("event") == "task.retry"]
    assert retries == []
    failures = [r for r in caplog.records if r.__dict__.get("event") == "task.failed"]
    assert [r.__dict__["exc_type"] for r in failures] == ["ValueError"]


@pytest.mark.usefixtures("worker")
def test_pickle_messages_are_never_executed(celery_app: Any) -> None:
    result = celery_app.send_task(PING_TASK, serializer="pickle")
    with pytest.raises(ResultTimeoutError):
        result.get(timeout=3)
    assert result.state == "PENDING"


def test_messages_wait_in_the_queue_without_a_worker(celery_app: Any) -> None:
    result = celery_app.send_task(PING_TASK)
    with pytest.raises(ResultTimeoutError):
        result.get(timeout=1)
    assert result.state == "PENDING"


@pytest.mark.usefixtures("worker")
def test_cli_worker_check_against_real_worker(settings: Settings, namespace: str, queue: str) -> None:
    out, err = io.StringIO(), io.StringIO()
    code = main(
        ["worker", "check", "--simulate-failures", "1", "--timeout", "30"],
        settings=settings,
        celery_app_factory=lambda s: create_celery_app(s, namespace=namespace, queue=queue),
        stdout=out,
        stderr=err,
    )
    assert code == 0, err.getvalue()
    assert "state: SUCCESS; attempts: 2" in out.getvalue()


def test_cli_worker_check_times_out_without_worker(settings: Settings, namespace: str, queue: str) -> None:
    out, err = io.StringIO(), io.StringIO()
    code = main(
        ["worker", "check", "--timeout", "1"],
        settings=settings,
        celery_app_factory=lambda s: create_celery_app(s, namespace=namespace, queue=queue),
        stdout=out,
        stderr=err,
    )
    assert code == 1
    assert "is the worker running?" in err.getvalue()
