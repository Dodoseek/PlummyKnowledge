"""The ingestion Celery task is a thin adapter: outcome mapping and retry backoff."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from plummy.application.ingestion.pipeline import RunOutcome
from plummy.workers.app import INGESTION_QUEUE, INGESTION_TASK, celery_config
from plummy.workers.tasks import ingestion
from tests.conftest import make_settings


class RetryError(Exception):
    pass


class FakeTask:
    def __init__(self, retries: int) -> None:
        self.request = SimpleNamespace(retries=retries)
        self.countdowns: list[int] = []

    def retry(self, *, countdown: int) -> RetryError:
        self.countdowns.append(countdown)
        return RetryError()


def outcome_of(monkeypatch: pytest.MonkeyPatch, outcome: RunOutcome) -> list[UUID]:
    seen: list[UUID] = []

    async def fake_run(job_id: UUID) -> RunOutcome:
        seen.append(job_id)
        return outcome

    monkeypatch.setattr(ingestion, "run_job", fake_run)
    return seen


@pytest.mark.parametrize("outcome", [o for o in RunOutcome if o is not RunOutcome.RETRY])
def test_final_outcomes_are_returned(monkeypatch: pytest.MonkeyPatch, outcome: RunOutcome) -> None:
    job_id = uuid4()
    seen = outcome_of(monkeypatch, outcome)
    assert ingestion._run_job(FakeTask(0), str(job_id)) == outcome.value
    assert seen == [job_id]


@pytest.mark.parametrize(("retries", "countdown"), [(0, 10), (1, 30), (4, 600), (15, 600)])
def test_retry_with_backoff(monkeypatch: pytest.MonkeyPatch, retries: int, countdown: int) -> None:
    outcome_of(monkeypatch, RunOutcome.RETRY)
    task = FakeTask(retries)
    with pytest.raises(RetryError):
        ingestion._run_job(task, str(uuid4()))
    assert task.countdowns == [countdown]


def test_message_must_be_a_job_id(monkeypatch: pytest.MonkeyPatch) -> None:
    seen = outcome_of(monkeypatch, RunOutcome.SUCCEEDED)
    with pytest.raises(ValueError, match="badly formed"):
        ingestion._run_job(FakeTask(0), "../../etc/passwd")
    assert seen == []


def test_ingestion_task_is_routed_to_its_queue() -> None:
    config: dict[str, Any] = celery_config(make_settings())
    assert config["task_routes"] == {INGESTION_TASK: {"queue": INGESTION_QUEUE}}
    assert config["accept_content"] == ["json"]
