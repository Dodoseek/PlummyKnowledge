"""Stopping a generation and the chat maintenance sweep (ADR-019 §3, Consequences)."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Final
from uuid import UUID

from plummy.application.errors import NotFoundError, ServiceUnavailableError
from plummy.application.ports import (
    Clock,
    GenerationControl,
    GenerationControlUnavailableError,
    UnitOfWorkFactory,
)
from plummy.application.security import SecurityContext
from plummy.domain.chat import RunStatus

logger = logging.getLogger(__name__)

RUN_NOT_FOUND: Final = "Generation not found."
INTERRUPTED: Final = "interrupted"
PURGE_BATCH: Final = 500


class StopGeneration:
    """``POST /chat/runs/{id}/stop``: only the owner of the conversation; anyone else gets ``404`` as for a
    run that does not exist. A finished run, or a second request, is accepted without effect."""

    def __init__(self, uow: UnitOfWorkFactory, control: GenerationControl, *, timeout_seconds: int) -> None:
        self._uow, self._control = uow, control
        self._flag_ttl = timeout_seconds + 60

    async def execute(self, context: SecurityContext, run_id: UUID) -> None:
        async with self._uow() as uow:
            run = await uow.generation_runs.get_owned(run_id, context.user_id)
        if run is None:
            raise NotFoundError(RUN_NOT_FOUND)
        if run.status is not RunStatus.RUNNING:
            return
        try:
            await self._control.request_stop(run.id, ttl_seconds=self._flag_ttl)
        except GenerationControlUnavailableError:
            raise ServiceUnavailableError() from None


class SweepChat:
    """Periodic (``scheduler``): answers left ``streaming`` by an API process that died become ``failed``
    (``interrupted``); deleted conversations are removed with their messages and runs."""

    def __init__(self, uow: UnitOfWorkFactory, clock: Clock, *, timeout_seconds: int) -> None:
        self._uow, self._clock = uow, clock
        # Well past the hard limit, so a live generation is never touched.
        self._stale_after = timedelta(seconds=timeout_seconds * 2 + 60)

    async def execute(self) -> int:
        now = self._clock.now()
        async with self._uow() as uow:
            stale = await uow.messages.fail_streaming(
                started_before=now - self._stale_after, error_code=INTERRUPTED, at=now
            )
            await uow.generation_runs.fail_for_messages(stale, error_code=INTERRUPTED, at=now)
            purged = await uow.conversations.purge_deleted(limit=PURGE_BATCH)
            await uow.commit()
        if stale or purged:
            logger.info("chat sweep", extra={"interrupted": len(stale), "conversations_purged": purged})
        return len(stale) + purged
