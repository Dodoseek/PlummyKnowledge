"""Celery configuration, the diagnostic task, lifecycle logs and ``worker check`` (no broker needed)."""

from __future__ import annotations

import io
import json
import logging
from types import SimpleNamespace
from typing import Any

import pytest
from kombu.exceptions import OperationalError

from plummy.cli.app import main
from plummy.core.logging import JsonFormatter, configure_logging
from plummy.workers.app import DEFAULT_NAMESPACE, celery_config, create_celery_app
from plummy.workers.errors import TransientJobError
from plummy.workers.observability import _on_failure, _on_postrun, _on_prerun, _on_retry
from plummy.workers.tasks.system import MAX_SIMULATED_FAILURES, PING_TASK, _ping
from tests.conftest import VALID_REDIS_URL, make_settings

# --- configuration -----------------------------------------------------------------------------------------


def test_messages_and_results_are_json_only() -> None:
    config = celery_config(make_settings())
    assert config["task_serializer"] == "json"
    assert config["result_serializer"] == "json"
    assert config["accept_content"] == ["json"]
    assert config["result_accept_content"] == ["json"]


def test_tasks_survive_worker_crash_and_results_expire() -> None:
    config = celery_config(make_settings())
    assert config["task_acks_late"] is True
    assert config["task_reject_on_worker_lost"] is True
    assert config["worker_prefetch_multiplier"] == 1
    assert config["result_expires"] == 24 * 60 * 60
    # Redelivery must not happen while the longest allowed task is still running.
    assert config["broker_transport_options"]["visibility_timeout"] > config["task_time_limit"]
    assert config["task_soft_time_limit"] < config["task_time_limit"]


def test_redis_keys_are_namespaced() -> None:
    config = celery_config(make_settings())
    assert config["broker_transport_options"]["global_keyprefix"] == DEFAULT_NAMESPACE
    assert config["result_backend_transport_options"]["global_keyprefix"] == DEFAULT_NAMESPACE
    custom = celery_config(make_settings(), namespace="plummy:test:", queue="q1")
    assert custom["broker_transport_options"]["global_keyprefix"] == "plummy:test:"
    assert custom["task_default_queue"] == "q1"


def test_celery_does_not_take_over_logging() -> None:
    config = celery_config(make_settings())
    assert config["worker_hijack_root_logger"] is False
    assert config["worker_redirect_stdouts"] is False


def test_app_uses_redis_url_from_settings_and_registers_ping() -> None:
    app = create_celery_app(make_settings())
    try:
        assert app.conf.broker_url == VALID_REDIS_URL
        assert app.conf.result_backend == VALID_REDIS_URL
        app.loader.import_default_modules()
        assert PING_TASK in app.tasks
    finally:
        app.close()


# --- diagnostic task ---------------------------------------------------------------------------------------


def _task(retries: int = 0) -> Any:
    return SimpleNamespace(request=SimpleNamespace(retries=retries))


def test_ping_succeeds_on_first_attempt() -> None:
    assert _ping(_task()) == {"status": "ok", "attempts": 1}


def test_ping_fails_transiently_for_the_first_n_attempts() -> None:
    with pytest.raises(TransientJobError):
        _ping(_task(retries=0), simulate_failures=2)
    with pytest.raises(TransientJobError):
        _ping(_task(retries=1), simulate_failures=2)
    assert _ping(_task(retries=2), simulate_failures=2) == {"status": "ok", "attempts": 3}


def test_ping_accepts_the_maximum_number_of_failures() -> None:
    result = _ping(_task(retries=MAX_SIMULATED_FAILURES), simulate_failures=MAX_SIMULATED_FAILURES)
    assert result["attempts"] == MAX_SIMULATED_FAILURES + 1


@pytest.mark.parametrize("value", [-1, MAX_SIMULATED_FAILURES + 1, 10**6])
def test_ping_rejects_out_of_range_failures(value: int) -> None:
    with pytest.raises(ValueError, match="between"):
        _ping(_task(), simulate_failures=value)


@pytest.mark.parametrize("value", [True, "1", 1.0, None])
def test_ping_rejects_non_integer_failures(value: object) -> None:
    with pytest.raises(TypeError):
        _ping(_task(), simulate_failures=value)  # type: ignore[arg-type]


# --- lifecycle logs ----------------------------------------------------------------------------------------

SECRET_PAYLOAD = "document-text-must-not-be-logged"


def _formatted(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    formatter = JsonFormatter()
    records = [record for record in caplog.records if record.name == "plummy.workers"]
    return [json.loads(formatter.format(record)) for record in records]


def test_lifecycle_events_have_ids_and_duration_but_no_payload(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="plummy.workers")
    task = SimpleNamespace(name=PING_TASK, request=SimpleNamespace(retries=0))
    _on_prerun(task_id="t-1", task=task, args=(SECRET_PAYLOAD,), kwargs={"text": SECRET_PAYLOAD})
    _on_postrun(task_id="t-1", task=task, state="SUCCESS", retval={"text": SECRET_PAYLOAD})

    started, finished = _formatted(caplog)
    assert started["event"] == "task.started"
    assert started["task_id"] == "t-1"
    assert started["task_name"] == PING_TASK
    assert started["attempt"] == 1
    assert finished["event"] == "task.finished"
    assert finished["state"] == "SUCCESS"
    assert finished["level"] == "INFO"
    assert isinstance(finished["duration_ms"], int)
    assert SECRET_PAYLOAD not in caplog.text
    assert SECRET_PAYLOAD not in json.dumps([started, finished])


def test_unsuccessful_finish_is_a_warning_and_unknown_start_has_no_duration(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="plummy.workers")
    task = SimpleNamespace(name=PING_TASK, request=SimpleNamespace(retries=1))
    _on_postrun(task_id="never-started", task=task, state="RETRY", retval=None)
    (finished,) = _formatted(caplog)
    assert finished["level"] == "WARNING"
    assert finished["attempt"] == 2
    assert finished["duration_ms"] is None


def test_retry_and_failure_log_exception_type_without_message(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="plummy.workers")
    sender = SimpleNamespace(name=PING_TASK)
    _on_retry(
        sender=sender,
        request=SimpleNamespace(id="t-2", retries=0),
        reason=TransientJobError(SECRET_PAYLOAD),
    )
    _on_failure(sender=sender, task_id="t-2", exception=ValueError(SECRET_PAYLOAD))

    retry, failure = _formatted(caplog)
    assert retry == {**retry, "event": "task.retry", "task_id": "t-2", "exc_type": "TransientJobError"}
    assert failure == {**failure, "event": "task.failed", "level": "ERROR", "exc_type": "ValueError"}
    assert SECRET_PAYLOAD not in caplog.text


def test_quiet_loggers_are_raised_to_critical() -> None:
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    trace = logging.getLogger("celery.app.trace")
    saved_trace_level = trace.level
    try:
        configure_logging("INFO", quiet_loggers=("celery.app.trace",))
        assert trace.level == logging.CRITICAL
        assert not trace.isEnabledFor(logging.ERROR)
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        trace.setLevel(saved_trace_level)


# --- worker check (CLI) ------------------------------------------------------------------------------------


class FakeResult:
    def __init__(self, state: str, value: object = None, *, timeout: bool = False) -> None:
        self.id = "task-123"
        self.state = state
        self._value = value
        self._timeout = timeout
        self.forgotten = False

    def get(self, *, timeout: float, propagate: bool) -> object:
        assert propagate is False
        if self._timeout:
            from celery.exceptions import TimeoutError as ResultTimeoutError

            raise ResultTimeoutError("timed out")
        return self._value

    def forget(self) -> None:
        self.forgotten = True


class FakeApp:
    def __init__(self, result: FakeResult | None = None, error: Exception | None = None) -> None:
        self.result = result
        self.error = error
        self.sent: list[tuple[str, dict[str, object]]] = []
        self.closed = False

    def send_task(self, name: str, *, kwargs: dict[str, object]) -> FakeResult:
        self.sent.append((name, kwargs))
        if self.error is not None:
            raise self.error
        assert self.result is not None
        return self.result

    def close(self) -> None:
        self.closed = True


def _check(app: FakeApp, *argv: str) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    code = main(
        ["worker", "check", *argv],
        settings=make_settings(),
        celery_app_factory=lambda _settings: app,
        stdout=out,
        stderr=err,
    )
    return code, out.getvalue(), err.getvalue()


def test_worker_check_reports_success_and_cleans_up() -> None:
    result = FakeResult("SUCCESS", {"status": "ok", "attempts": 2})
    app = FakeApp(result)
    code, out, err = _check(app, "--simulate-failures", "1")
    assert code == 0, err
    assert app.sent == [(PING_TASK, {"simulate_failures": 1})]
    assert "task task-123 sent" in out
    assert "state: SUCCESS; attempts: 2" in out
    assert result.forgotten
    assert app.closed


def test_worker_check_fails_when_task_fails() -> None:
    app = FakeApp(FakeResult("FAILURE", ValueError("boom")))
    code, _, err = _check(app)
    assert code == 1
    assert "task failure (ValueError)" in err
    assert "boom" not in err
    assert app.closed


def test_worker_check_fails_on_timeout() -> None:
    app = FakeApp(FakeResult("PENDING", timeout=True))
    code, _, err = _check(app, "--timeout", "0.5")
    assert code == 1
    assert "no result within 0.5s (state PENDING)" in err
    assert app.closed


def test_worker_check_reports_unavailable_broker() -> None:
    app = FakeApp(error=OperationalError("connection refused to redis://:secret@redis"))
    code, _, err = _check(app)
    assert code == 1
    assert err.strip() == "error: broker unavailable (OperationalError)"
    assert app.closed


@pytest.mark.parametrize(
    "argv",
    [
        ["--simulate-failures", str(MAX_SIMULATED_FAILURES + 1)],
        ["--simulate-failures", "-1"],
        ["--timeout", "0"],
        ["--timeout", "601"],
        ["--timeout", "soon"],
    ],
)
def test_worker_check_rejects_invalid_arguments(argv: list[str]) -> None:
    app = FakeApp(FakeResult("SUCCESS", {}))
    with pytest.raises(SystemExit) as exc_info:
        _check(app, *argv)
    assert exc_info.value.code == 2
    assert app.sent == []
