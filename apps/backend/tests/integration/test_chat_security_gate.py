"""Security gate of Phase 8 (DEVELOPMENT_ROADMAP.md): CSRF and Origin on every state-changing chat request,
private chat data never cached, the conversation text never in the logs.

Rendering of answers (no raw HTML, no external images) is tested in the frontend (AnswerMarkdown.test.tsx),
the viewer's access re-check in test_source_viewer.py and SC-01 from the browser in apps/e2e.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx
import pytest

from plummy.api.security import CSRF_HEADER
from tests.integration.test_chat_stream import (
    URL,
    Chat,
    body,
    chat,
    env,
    events,
    named,
    send,
    wait_for,
)

pytestmark = pytest.mark.integration

__all__ = ["chat", "env"]  # fixtures used by the tests below

FOREIGN_ORIGIN = "https://evil.example"


async def without_csrf(client: httpx.AsyncClient, method: str, url: str, **kwargs: Any) -> httpx.Response:
    request = client.build_request(method, url, **kwargs)
    del request.headers[CSRF_HEADER]
    return await client.send(request)


def refused(response: httpx.Response) -> None:
    assert response.status_code == 403, response.text
    assert response.json()["code"] == "csrf_failed"


async def counts(chat: Chat) -> tuple[Any, ...]:
    [row] = await chat.env.harness.sql(
        "SELECT (SELECT count(*) FROM conversations WHERE deleted_at IS NULL), "
        "(SELECT count(*) FROM messages), (SELECT count(*) FROM generation_runs), "
        "(SELECT string_agg(title, ',' ORDER BY title) FROM conversations)"
    )
    return tuple(row)


async def test_every_chat_write_needs_the_csrf_token_and_a_trusted_origin(chat: Chat) -> None:
    first = events(await send(chat))
    conversation = named(first, "message_start")[0]["conversation_id"]
    run = named(first, "message_start")[0]["generation_run_id"]
    before = await counts(chat)
    requests_before = len(chat.llm.requests)

    writes: list[tuple[str, str, dict[str, Any]]] = [
        ("POST", URL, {"json": body(chat)}),
        ("POST", URL, {"json": body(chat, conversation_id=conversation, assistant_id=None)}),
        ("POST", f"/api/v1/chat/runs/{run}/stop", {}),
        ("PATCH", f"/api/v1/conversations/{conversation}", {"json": {"title": "Changed"}}),
        ("DELETE", f"/api/v1/conversations/{conversation}", {}),
    ]
    for method, url, kwargs in writes:
        refused(await without_csrf(chat.member, method, url, **kwargs))
        refused(await chat.member.request(method, url, headers={CSRF_HEADER: "forged"}, **kwargs))
        # The right token from another site's page: the Origin check refuses it too (ADR-013).
        refused(await chat.member.request(method, url, headers={"Origin": FOREIGN_ORIGIN}, **kwargs))

    assert await counts(chat) == before
    assert len(chat.llm.requests) == requests_before, "a refused request must not reach the model"


async def test_a_refused_stream_does_not_take_a_generation_slot_or_the_rate_limit(chat: Chat) -> None:
    for _ in range(25):  # more than the chat limit (20/min) and the slots (2)
        refused(await without_csrf(chat.member, "POST", URL, json=body(chat)))
    assert (await send(chat)).status_code == 200


async def test_chat_data_is_never_cached(chat: Chat) -> None:
    response = await send(chat)
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "no-store" in response.headers["cache-control"]
    assert response.headers["x-accel-buffering"] == "no"
    conversation = named(events(response), "message_start")[0]["conversation_id"]
    for url in (
        "/api/v1/conversations",
        f"/api/v1/conversations/{conversation}",
        f"/api/v1/conversations/{conversation}/messages",
    ):
        reply = await chat.member.get(url)
        assert reply.status_code == 200, reply.text
        assert "no-store" in reply.headers.get("cache-control", ""), url


async def test_the_conversation_text_is_never_logged(chat: Chat, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    question = "Сколько дней отпуска можно перенести? Мой пароль Hunter-2-secret"
    chat.llm.hold = True
    streaming = asyncio.create_task(send(chat, content=question))
    await asyncio.wait_for(chat.llm.started.wait(), 10)
    [(run_id,)] = await wait_for(
        lambda: chat.env.harness.sql("SELECT id FROM generation_runs WHERE status = 'running'")
    )
    assert (await chat.member.post(f"/api/v1/chat/runs/{run_id}/stop")).status_code == 202
    stream = events(await asyncio.wait_for(streaming, 10))
    assert named(stream, "message_complete")

    assert caplog.records, "the request must have been logged at all (positive control)"
    text = caplog.text
    for secret in ("Hunter-2-secret", "Сколько дней", "До пяти дней", *chat.documents.values()):
        assert secret not in text
    for record in caplog.records:
        assert "Hunter-2-secret" not in repr(record.__dict__)
