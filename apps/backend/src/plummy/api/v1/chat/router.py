"""``POST /api/v1/chat/stream`` and ``POST /api/v1/chat/runs/{id}/stop`` (API_CONTRACTS.md §3, ADR-019).

Everything that can be refused is refused before the response starts, as ordinary problem details; after
``200`` the outcome travels as events. A heartbeat keeps proxies from closing a quiet connection.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Final
from uuid import UUID

from fastapi import APIRouter, Request, Response, status
from fastapi.responses import StreamingResponse

from plummy.api.dependencies import ResourcesDep
from plummy.api.security import CurrentContext
from plummy.api.v1.chat.schemas import ChatStreamRequest, event_frame, heartbeat_frame
from plummy.application.chat.send import ChatEvent, SendCommand

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chat", tags=["chat"])

HEARTBEAT_SECONDS: Final = 15.0
STREAM_HEADERS: Final = {
    "Cache-Control": "no-cache, no-store",
    "X-Accel-Buffering": "no",  # an nginx in front must not buffer the stream (ADR-019)
}


async def _with_heartbeat(
    events: AsyncIterator[ChatEvent], interval: float
) -> AsyncIterator[ChatEvent | None]:
    """The events, with ``None`` after every ``interval`` seconds without one."""
    pending: asyncio.Future[ChatEvent] | None = None
    try:
        while True:
            pending = asyncio.ensure_future(anext(events))
            while not pending.done():
                await asyncio.wait({pending}, timeout=interval)
                if not pending.done():
                    yield None
            try:
                event = pending.result()
            except StopAsyncIteration:
                return
            finally:
                pending = None
            yield event
    finally:
        if pending is not None and not pending.done():
            # The client went away while an event was awaited: the generation sees the cancellation.
            pending.cancel()
        else:
            closer = getattr(events, "aclose", None)
            if closer is not None:
                await closer()


async def _frames(events: AsyncIterator[ChatEvent], correlation_id: str | None) -> AsyncIterator[bytes]:
    try:
        async for event in _with_heartbeat(events, HEARTBEAT_SECONDS):
            if event is None:
                yield heartbeat_frame(datetime.now(UTC))
            else:
                yield event_frame(event, correlation_id=correlation_id)
    except Exception as exc:  # e.g. the database failed while saving: the stream just ends
        logger.error("chat stream failed", extra={"error_type": type(exc).__name__})


@router.post("/stream", response_class=StreamingResponse)
async def chat_stream(
    body: ChatStreamRequest, request: Request, context: CurrentContext, resources: ResourcesDep
) -> StreamingResponse:
    generation = await resources.chat.send.prepare(
        context,
        SendCommand(
            conversation_id=body.conversation_id,
            assistant_id=body.assistant_id,
            content=body.content,
            client_message_id=body.client_message_id,
        ),
    )
    correlation_id = getattr(request.state, "request_id", None)
    return StreamingResponse(
        _frames(generation.events(), correlation_id if isinstance(correlation_id, str) else None),
        media_type="text/event-stream",
        headers=STREAM_HEADERS,
    )


@router.post("/runs/{run_id}/stop", status_code=status.HTTP_202_ACCEPTED)
async def stop_generation(run_id: UUID, context: CurrentContext, resources: ResourcesDep) -> Response:
    await resources.chat.stop.execute(context, run_id)
    return Response(status_code=status.HTTP_202_ACCEPTED, headers={"Cache-Control": "no-store"})
