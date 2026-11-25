"""``POST /api/v1/chat/stream`` and stopping a generation (API_CONTRACTS.md §3, ADR-019, ADR-005).

Real app, PostgreSQL and Redis. Only the provider gateway is replaced: the deterministic embedder of the
retrieval tests and a scripted chat model that records what it was sent. Access policy, rate limits,
generation slots, the citation filter and the SQL are the production ones.
"""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from plummy.application.ai.ports import (
    CancellationToken,
    ChatRequest,
    ChatStreamEvent,
    Finished,
    FinishReason,
    LLMCapabilities,
    LLMProvider,
    ProviderConnection,
    ProviderError,
    ProviderTimeoutError,
    TextDelta,
    UsageReport,
)
from plummy.application.chat.runs import SweepChat
from plummy.application.chat.send import SendCommand
from plummy.application.ports import GenerationControlUnavailableError
from plummy.application.retrieval.context import NO_SOURCES_NOTE
from plummy.infrastructure.clock import SystemClock
from plummy.infrastructure.db.unit_of_work import SqlAlchemyUnitOfWorkFactory
from plummy.infrastructure.redis.generation_control import KEY_PREFIX as CHAT_PREFIX
from plummy.infrastructure.redis.rate_limiter import KEY_PREFIX as LIMIT_PREFIX
from plummy.infrastructure.storage.local import LocalFileStorage
from tests.integration.harness import running_auth_app
from tests.integration.test_assistants_api import Env, create, make_env, signed_in
from tests.integration.test_conversations_api import grant_read, problem
from tests.integration.test_document_upload import Env as DocumentEnv
from tests.integration.test_indexing import FakeGateway
from tests.integration.test_retrieval import EXPENSES, LEAVE, SECRET, TextEmbedder, context, indexed

pytestmark = pytest.mark.integration

URL = "/api/v1/chat/stream"
QUESTION = "Сколько дней отпуска можно перенести?"


@dataclass
class ScriptedLLM:
    """Streams ``deltas``, then finishes; ``hold`` waits after the first delta until it is cancelled."""

    deltas: list[str] = field(default_factory=lambda: ["До пяти дней [c1]", " можно перенести[c9]."])
    finish: FinishReason = FinishReason.STOP
    error: ProviderError | None = None
    hold: bool = False
    requests: list[ChatRequest] = field(default_factory=list)
    started: asyncio.Event = field(default_factory=asyncio.Event)
    cancelled: bool = False

    def capabilities(self) -> LLMCapabilities:
        return LLMCapabilities()

    async def test_connection(self, *, timeout: float) -> Any:  # noqa: ASYNC109
        raise NotImplementedError

    async def stream_chat(
        self,
        request: ChatRequest,
        *,
        cancel: CancellationToken,
        timeout: float,  # noqa: ASYNC109 - the port's signature
    ) -> AsyncIterator[ChatStreamEvent]:
        self.requests.append(request)
        self.started.set()
        try:
            for number, delta in enumerate(self.deltas):
                yield TextDelta(delta)
                if self.hold and number == 0:
                    await asyncio.Event().wait()  # until cancelled
            if self.error is not None:
                raise self.error
            yield Finished(self.finish, UsageReport(input_tokens=11, output_tokens=7))
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class ChatGateway(FakeGateway):
    def __init__(self, embedder: TextEmbedder, llm: ScriptedLLM) -> None:
        super().__init__(embedder)
        self.chat = llm

    def llm(self, connection: ProviderConnection) -> LLMProvider:
        return self.chat


@dataclass
class Chat:
    env: Env
    assistant: str
    member: httpx.AsyncClient
    member_id: str
    llm: ScriptedLLM
    documents: dict[str, str]
    secret_document: str


@pytest.fixture
async def env(
    migrated_database_url: str, redis_url: str, clean_rate_limits: None, tmp_path: Path
) -> AsyncIterator[Env]:
    async with running_auth_app(migrated_database_url, redis_url, tmp_path / "bootstrap") as harness:
        env = await make_env(harness)
        yield env
        await clean_chat_keys(env)


async def clean_chat_keys(env: Env) -> None:
    redis = env.harness.resources.redis
    keys = [key async for key in redis.scan_iter(match=f"{CHAT_PREFIX}*")]
    if keys:
        await redis.delete(*keys)


@pytest.fixture
async def chat(env: Env, monkeypatch: pytest.MonkeyPatch) -> Chat:
    embedder, llm = TextEmbedder(), ScriptedLLM()
    gateway = ChatGateway(embedder, llm)
    resources = env.harness.resources
    monkeypatch.setattr(resources.knowledge.retrieve, "_gateway", gateway)
    monkeypatch.setattr(resources.chat.send, "_gateway", gateway)
    storage = LocalFileStorage(env.harness.settings.storage_path)
    hr = DocumentEnv(env.harness, env.admin, env.hr, storage)
    documents = {
        "leave": await indexed(hr, env.hr, "leave.md", LEAVE, embedder),
        "expenses": await indexed(hr, env.hr, "expenses.md", EXPENSES, embedder),
    }
    it = DocumentEnv(env.harness, env.admin, env.it, storage)
    secret = await indexed(it, env.it, "salaries.md", SECRET, embedder)
    assistant = await create(env, name="HR", knowledge_base_ids=[env.hr])
    member, member_id = await signed_in(env, "member@example.com", ["member"])
    await grant_read(env, env.hr, member_id)
    return Chat(env, assistant["id"], member, member_id, llm, documents, secret)


def body(chat: Chat, **overrides: Any) -> dict[str, Any]:
    return {
        "conversation_id": None,
        "assistant_id": chat.assistant,
        "content": QUESTION,
        "client_message_id": str(uuid4()),
        **overrides,
    }


def events(response: httpx.Response) -> list[tuple[str, dict[str, Any]]]:
    assert response.status_code == 200, response.text
    parsed = []
    for raw in response.text.split("\n\n"):
        if not raw.strip():
            continue
        lines = dict(line.split(": ", 1) for line in raw.split("\n"))
        assert set(lines) == {"event", "data"}, raw  # no ``id:`` (ADR-019 §5)
        parsed.append((lines["event"], json.loads(lines["data"])))
    return parsed


def named(stream: list[tuple[str, dict[str, Any]]], name: str) -> list[dict[str, Any]]:
    return [data for event, data in stream if event == name]


async def send(chat: Chat, client: httpx.AsyncClient | None = None, **overrides: Any) -> httpx.Response:
    return await (client or chat.member).post(URL, json=body(chat, **overrides))


# --- a complete answer ------------------------------------------------------------------------------------


async def test_answer_streams_with_validated_citations(chat: Chat) -> None:
    response = await send(chat)
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["x-accel-buffering"] == "no"
    assert "no-store" in response.headers["cache-control"]
    stream = events(response)
    names = [event for event, _ in stream]
    assert names[0] == "message_start"
    assert names[-1] == "message_complete"
    assert "error" not in names
    start = named(stream, "message_start")[0]
    text = "".join(data["delta"] for data in named(stream, "token"))
    assert text == "До пяти дней [c1] можно перенести."  # [c9] was never issued
    [citation] = named(stream, "citation")
    assert citation["citation_id"] == "c1"
    assert citation["document_id"] in chat.documents.values()
    assert citation["viewer_route"] == (
        f"/viewer/documents/{citation['document_id']}/versions/{citation['document_version_id']}"
        f"?chunk={citation['chunk_id']}"
    )
    complete = named(stream, "message_complete")[0]
    assert complete == {
        "assistant_message_id": start["assistant_message_id"],
        "finish_reason": "stop",
        "citation_ids": ["c1"],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7},
    }

    harness = chat.env.harness
    rows = await harness.sql(
        "SELECT role, status, content, error_code FROM messages "
        "WHERE conversation_id = :id ORDER BY sequence",
        id=start["conversation_id"],
    )
    assert rows == [("user", "completed", QUESTION, None), ("assistant", "completed", text, None)]
    [(title,)] = await harness.sql(
        "SELECT title FROM conversations WHERE id = :id", id=start["conversation_id"]
    )
    assert title == QUESTION
    [saved] = await harness.sql(
        "SELECT citation_key, chunk_id, viewer_route FROM message_citations WHERE message_id = :id",
        id=start["assistant_message_id"],
    )
    assert saved == ("c1", UUID(citation["chunk_id"]), citation["viewer_route"])
    [run] = await harness.sql(
        "SELECT status, finish_reason, rejected_citation_count, prompt_tokens, completion_tokens, "
        "cardinality(retrieved_chunk_ids), error_code, retrieval_ms IS NOT NULL, total_ms IS NOT NULL, "
        "embedding_config_id IS NOT NULL FROM generation_runs WHERE id = :id",
        id=start["generation_run_id"],
    )
    assert run[:3] == ("completed", "stop", 1)
    assert run[3:5] == (11, 7)
    assert run[5] >= 1
    assert run[6] is None
    assert all(run[7:])
    # The slot is free again.
    redis = chat.env.harness.resources.redis
    assert await redis.zcard(f"{CHAT_PREFIX}active:{UUID(chat.member_id).hex}") == 0


async def test_only_the_assistants_knowledge_bases_reach_the_model(chat: Chat) -> None:
    """SC-01 through chat: the IT document is never retrieved, even when asked for word by word."""
    await send(chat, content="The salary review happens in March for every employee.")
    [request] = chat.llm.requests
    assert request.context, "retrieval found the HR documents"
    rows = await chat.env.harness.sql(
        "SELECT DISTINCT d.id FROM generation_runs r, unnest(r.retrieved_chunk_ids) AS c(id) "
        "JOIN document_chunks k ON k.id = c.id JOIN documents d ON d.id = k.document_id"
    )
    assert {str(document_id) for (document_id,) in rows} <= set(chat.documents.values())
    assert all("salary" not in document.text for document in request.context)
    # Rules come first and apart from the retrieved text.
    assert request.instructions.startswith("You answer questions using only the documents")
    assert request.messages[-1].content == "The salary review happens in March for every employee."


async def test_a_follow_up_carries_history_without_old_citation_ids(chat: Chat) -> None:
    first = named(events(await send(chat)), "message_start")[0]
    follow_up = "Можно больше?"
    response = await send(
        chat, conversation_id=first["conversation_id"], assistant_id=None, content=follow_up
    )
    second = named(events(response), "message_start")[0]
    assert second["conversation_id"] == first["conversation_id"]
    history = [(m.role, m.content) for m in chat.llm.requests[1].messages]
    assert history == [
        ("user", QUESTION),
        ("assistant", "До пяти дней можно перенести."),
        ("user", follow_up),
    ]
    sequences = await chat.env.harness.sql(
        "SELECT sequence, role FROM messages WHERE conversation_id = :id ORDER BY sequence",
        id=first["conversation_id"],
    )
    assert sequences == [(1, "user"), (2, "assistant"), (3, "user"), (4, "assistant")]


async def test_an_existing_untitled_conversation_takes_the_first_question_as_title(chat: Chat) -> None:
    created = (await chat.member.post("/api/v1/conversations", json={"assistant_id": chat.assistant})).json()
    events(await send(chat, conversation_id=created["id"], assistant_id=None))
    fetched = (await chat.member.get(f"/api/v1/conversations/{created['id']}")).json()
    assert fetched["title"] == QUESTION


async def test_an_assistant_without_knowledge_bases_answers_without_sources_for_its_managers(
    chat: Chat,
) -> None:
    preview = await create(chat.env, name="Draft", knowledge_base_ids=[])
    stream = events(await send(chat, chat.env.admin, assistant_id=preview["id"]))
    assert named(stream, "citation") == []  # nothing was issued, so [c1] is removed too
    request = chat.llm.requests[-1]
    assert request.context == ()
    assert NO_SOURCES_NOTE in request.instructions
    [(embedding,)] = await chat.env.harness.sql(
        "SELECT embedding_config_id FROM generation_runs WHERE assistant_id = :id", id=preview["id"]
    )
    assert embedding is None


# --- refusals before the stream ---------------------------------------------------------------------------


async def test_the_same_client_message_id_is_not_answered_twice(chat: Chat) -> None:
    message_id = str(uuid4())
    start = named(events(await send(chat, client_message_id=message_id)), "message_start")[0]
    for overrides in ({}, {"conversation_id": start["conversation_id"], "assistant_id": None}):
        response = await send(chat, client_message_id=message_id, **overrides)
        payload = problem(response, 409, "message_already_sent")
        assert payload["user_message_id"] == start["user_message_id"]
        assert payload["assistant_message_id"] == start["assistant_message_id"]
    assert len(chat.llm.requests) == 1
    assert await chat.env.harness.sql("SELECT count(*) FROM messages") == [(2,)]
    assert await chat.env.harness.sql("SELECT count(*) FROM conversations") == [(1,)]


async def test_an_assistant_the_caller_cannot_use(chat: Chat) -> None:
    env = chat.env
    it_only = await create(env, name="IT", knowledge_base_ids=[env.it])
    off = await create(env, name="Off", knowledge_base_ids=[env.hr], status="disabled")
    for assistant_id in (it_only["id"], off["id"], str(uuid4())):
        problem(await send(chat, assistant_id=assistant_id), 404, "not_found")
    # A conversation of one's own whose assistant lost its access: 403, the history stays.
    start = named(events(await send(chat)), "message_start")[0]
    await grant_read(env, env.hr)  # nobody has access any more
    response = await send(chat, conversation_id=start["conversation_id"], assistant_id=None)
    problem(response, 403, "assistant_unavailable")
    assert len(chat.llm.requests) == 1
    assert await env.harness.sql("SELECT count(*) FROM messages") == [(2,)]


async def test_a_disabled_assistant_stops_an_existing_conversation(chat: Chat) -> None:
    start = named(events(await send(chat)), "message_start")[0]
    updated = await chat.env.admin.patch(f"/api/v1/assistants/{chat.assistant}", json={"status": "disabled"})
    assert updated.status_code == 200, updated.text
    response = await send(chat, conversation_id=start["conversation_id"], assistant_id=None)
    problem(response, 403, "assistant_unavailable")


async def test_someone_elses_conversation_does_not_exist(chat: Chat) -> None:
    start = named(events(await send(chat)), "message_start")[0]
    other, other_id = await signed_in(chat.env, "other@example.com", ["member"])
    await grant_read(chat.env, chat.env.hr, chat.member_id, other_id)
    response = await send(chat, other, conversation_id=start["conversation_id"], assistant_id=None)
    problem(response, 404, "not_found")
    # Not even an administrator can write into it.
    response = await send(chat, chat.env.admin, conversation_id=start["conversation_id"], assistant_id=None)
    problem(response, 404, "not_found")
    assert len(chat.llm.requests) == 1


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"knowledge_base_ids": ["x"]}, "body.knowledge_base_ids"),
        ({"assistant_id": None}, "body.assistant_id"),
        ({"content": "   "}, "body.content"),
        ({"content": "a\u0000b"}, "body.content"),
        ({"content": "x" * 4001}, "body.content"),
        ({"client_message_id": "nope"}, "body.client_message_id"),
    ],
)
async def test_invalid_requests(chat: Chat, overrides: dict[str, Any], field: str) -> None:
    response = await send(chat, **overrides)
    assert response.status_code == 422, response.text
    assert response.json()["errors"][0]["field"] == field
    assert chat.llm.requests == []


async def test_chat_use_is_required(chat: Chat) -> None:
    reader, reader_id = await signed_in(chat.env, "reader@example.com", [])
    await grant_read(chat.env, chat.env.hr, chat.member_id, reader_id)
    problem(await send(chat, reader), 403, "permission_denied")
    anonymous = chat.env.harness.new_client()
    assert (await anonymous.post(URL, json=body(chat))).status_code == 401


async def test_the_chat_limit_is_per_user(chat: Chat) -> None:
    redis = chat.env.harness.resources.redis
    await redis.set(f"{LIMIT_PREFIX}chat:user:{UUID(chat.member_id).hex}:1m", 20, ex=60)
    response = await send(chat)
    problem(response, 429, "rate_limited")
    assert int(response.headers["retry-after"]) >= 1
    assert chat.llm.requests == []
    assert await chat.env.harness.sql("SELECT count(*) FROM messages") == [(0,)]
    # Another user is not affected.
    events(await send(chat, chat.env.admin))


async def test_at_most_two_generations_at_once(chat: Chat) -> None:
    redis = chat.env.harness.resources.redis
    key = f"{CHAT_PREFIX}active:{UUID(chat.member_id).hex}"
    future_ms = int(time.time() * 1000) + 60_000
    await redis.zadd(key, {"a": future_ms, "b": future_ms})
    problem(await send(chat), 429, "rate_limited")
    assert await chat.env.harness.sql("SELECT count(*) FROM messages") == [(0,)]
    # A slot of a process that died expires on its own.
    await redis.zadd(key, {"b": int(time.time() * 1000) - 1})
    events(await send(chat))
    assert await redis.zrange(key, 0, -1) == ["a"]


async def test_a_disabled_chat_model_is_refused_before_the_stream(chat: Chat) -> None:
    env = chat.env
    disabled = await env.admin.patch(
        f"/api/v1/providers/{env.provider_id}/models/{env.model_id}", json={"enabled": False}
    )
    assert disabled.status_code == 200, disabled.text
    problem(await send(chat), 409, "model_unavailable")
    assert chat.llm.requests == []


# --- failures and stops after the stream started ----------------------------------------------------------


async def test_a_provider_error_ends_the_stream_with_an_event(chat: Chat) -> None:
    chat.llm.error = ProviderTimeoutError()
    stream = events(await send(chat))
    assert [event for event, _ in stream][-1] == "error"
    [error] = named(stream, "error")
    assert error["code"] == "provider_timeout"
    assert error["retryable"] is True
    assert error["correlation_id"]
    start = named(stream, "message_start")[0]
    [answer] = await chat.env.harness.sql(
        "SELECT status, content, error_code FROM messages WHERE id = :id", id=start["assistant_message_id"]
    )
    assert answer == ("failed", "До пяти дней [c1] можно перенести.", "provider_timeout")
    [run] = await chat.env.harness.sql(
        "SELECT status, finish_reason, error_code FROM generation_runs WHERE id = :id",
        id=start["generation_run_id"],
    )
    assert run == ("failed", "error", "provider_timeout")


async def test_a_content_filter_finish_is_a_failure(chat: Chat) -> None:
    chat.llm.finish = FinishReason.CONTENT_FILTER
    [error] = named(events(await send(chat)), "error")
    assert error["code"] == "provider_content_filtered"


async def test_the_hard_timeout_ends_a_slow_answer(chat: Chat, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat.env.harness.resources.chat.send, "timeout_seconds", 1)
    chat.llm.hold = True
    stream = events(await send(chat))
    [error] = named(stream, "error")
    assert error["code"] == "provider_timeout"
    assert chat.llm.cancelled
    [(status, content)] = await chat.env.harness.sql(
        "SELECT status, content FROM messages WHERE role = 'assistant'"
    )
    assert (status, content) == ("failed", "До пяти дней [c1]")


async def wait_for(check: Callable[[], Any], *, seconds: float = 5.0) -> Any:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = await check()
        if result:
            return result
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached")


async def test_stop_ends_the_answer_and_keeps_the_partial_text(chat: Chat) -> None:
    chat.llm.hold = True
    streaming = asyncio.create_task(send(chat))
    await asyncio.wait_for(chat.llm.started.wait(), 10)
    [(run_id,)] = await wait_for(
        lambda: chat.env.harness.sql("SELECT id FROM generation_runs WHERE status = 'running'")
    )
    stop = f"/api/v1/chat/runs/{run_id}/stop"
    other, _ = await signed_in(chat.env, "other@example.com", ["member"])
    problem(await other.post(stop), 404, "not_found")
    problem(await chat.env.admin.post(stop), 404, "not_found")
    problem(await chat.member.post(f"/api/v1/chat/runs/{uuid4()}/stop"), 404, "not_found")
    assert (await chat.member.post(stop)).status_code == 202
    stream = events(await asyncio.wait_for(streaming, 10))
    complete = named(stream, "message_complete")[0]
    assert complete["finish_reason"] == "user_cancelled"
    assert complete["citation_ids"] == ["c1"]
    assert chat.llm.cancelled
    [answer] = await chat.env.harness.sql(
        "SELECT m.status, m.content, r.status, r.finish_reason, r.error_code "
        "FROM messages m JOIN generation_runs r ON r.message_id = m.id"
    )
    assert answer == ("stopped", "До пяти дней [c1]", "stopped", "user_cancelled", None)
    assert await chat.env.harness.sql("SELECT count(*) FROM message_citations") == [(1,)]
    # Idempotent once finished.
    assert (await chat.member.post(stop)).status_code == 202


async def test_a_client_that_goes_away_cancels_the_generation(chat: Chat) -> None:
    """The response is cancelled or closed by the server when the connection drops; the same happens here
    by closing the event stream after the first token."""
    chat.llm.hold = True
    member = context(chat.member_id, "chat.use")
    generation = await chat.env.harness.resources.chat.send.prepare(
        member, SendCommand(None, UUID(chat.assistant), QUESTION, uuid4())
    )
    stream = generation.events()
    seen = [type(await anext(stream)).__name__ for _ in range(3)]
    assert seen == ["MessageStarted", "TokenDelta", "CitationIssued"]
    reading = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0.1)
    reading.cancel()
    with pytest.raises(asyncio.CancelledError):
        await reading
    [row] = await wait_for(
        lambda: chat.env.harness.sql(
            "SELECT m.status, m.content, r.status, r.error_code FROM messages m "
            "JOIN generation_runs r ON r.message_id = m.id WHERE m.status <> 'streaming'"
        )
    )
    assert row == ("stopped", "До пяти дней [c1]", "stopped", "client_disconnected")
    assert chat.llm.cancelled
    redis = chat.env.harness.resources.redis
    assert await redis.zcard(f"{CHAT_PREFIX}active:{UUID(chat.member_id).hex}") == 0


async def test_closing_the_stream_between_events_saves_it_too(chat: Chat) -> None:
    member = context(chat.member_id, "chat.use")
    generation = await chat.env.harness.resources.chat.send.prepare(
        member, SendCommand(None, UUID(chat.assistant), QUESTION, uuid4())
    )
    stream = generation.events()
    await anext(stream)
    await anext(stream)
    await stream.aclose()  # type: ignore[attr-defined]
    [row] = await chat.env.harness.sql("SELECT status, content FROM messages WHERE role = 'assistant'")
    assert row == ("stopped", "До пяти дней [c1]")


# --- maintenance ------------------------------------------------------------------------------------------


async def test_the_sweep_fails_abandoned_answers_and_purges_deleted_conversations(chat: Chat) -> None:
    harness = chat.env.harness
    old = named(events(await send(chat)), "message_start")[0]
    live = named(events(await send(chat)), "message_start")[0]
    gone = named(events(await send(chat)), "message_start")[0]
    # An answer whose process died long ago, and one still within the hard limit.
    for message_id, age in ((old["assistant_message_id"], 3600), (live["assistant_message_id"], 1)):
        await harness.sql(
            "UPDATE messages SET status = 'streaming', completed_at = NULL, "
            "created_at = now() - make_interval(secs => :age) WHERE id = :id",
            id=message_id,
            age=age,
        )
        await harness.sql(
            "UPDATE generation_runs SET status = 'running', finish_reason = NULL WHERE message_id = :id",
            id=message_id,
        )
    assert (await chat.member.delete(f"/api/v1/conversations/{gone['conversation_id']}")).status_code == 204

    uow = SqlAlchemyUnitOfWorkFactory(harness.resources.session_factory)
    sweep = SweepChat(uow, SystemClock(), timeout_seconds=120)
    assert await sweep.execute() == 2
    statuses = dict(
        await harness.sql(
            "SELECT m.id, m.status || '/' || coalesce(m.error_code, '') || '/' || r.status "
            "FROM messages m JOIN generation_runs r ON r.message_id = m.id"
        )
    )
    assert statuses[UUID(old["assistant_message_id"])] == "failed/interrupted/failed"
    assert statuses[UUID(live["assistant_message_id"])] == "streaming//running"
    assert await harness.sql(
        "SELECT count(*) FROM conversations WHERE id = :id", id=gone["conversation_id"]
    ) == [(0,)]
    assert await harness.sql(
        "SELECT count(*) FROM messages WHERE conversation_id = :id", id=gone["conversation_id"]
    ) == [(0,)]


async def test_without_generation_slots_nothing_starts(chat: Chat, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redis unavailable for the slots: ``503`` before the stream, the taken limit is not a generation."""

    async def unavailable(*args: Any, **kwargs: Any) -> bool:
        raise GenerationControlUnavailableError

    monkeypatch.setattr(chat.env.harness.resources.chat.send._control, "acquire", unavailable)
    problem(await send(chat), 503, "service_unavailable")
    assert chat.llm.requests == []
    assert await chat.env.harness.sql("SELECT count(*) FROM messages") == [(0,)]


async def test_over_a_real_connection_heartbeats_flow_and_a_disconnect_stops_the_answer(
    chat: Chat, monkeypatch: pytest.MonkeyPatch
) -> None:
    """uvicorn and a socket instead of the in-process transport, which buffers the whole response."""
    import socket

    import uvicorn

    from plummy.api.v1.chat import router as chat_router
    from tests.integration.conftest import TEST_ORIGIN

    monkeypatch.setattr(chat_router, "HEARTBEAT_SECONDS", 0.2)
    chat.llm.hold = True
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(chat.env.harness.app, host="127.0.0.1", port=port, lifespan="off", log_level="warning")
    )
    serving = asyncio.create_task(server.serve())
    try:
        await wait_for(lambda: asyncio.sleep(0, result=server.started))
        cookies = "; ".join(f"{name}={value}" for name, value in chat.member.cookies.items())
        headers = {**chat.member.headers, "Origin": TEST_ORIGIN, "Cookie": cookies}
        seen: list[str] = []
        async with (
            httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}", headers=headers) as client,
            client.stream("POST", URL, json=body(chat)) as response,
        ):
            assert response.status_code == 200
            async for line in response.aiter_lines():
                if line.startswith("event: "):
                    seen.append(line[7:])
                if seen.count("heartbeat") == 2:
                    break  # leaving the block closes the connection
        assert seen[:3] == ["message_start", "token", "citation"]
        [row] = await wait_for(
            lambda: chat.env.harness.sql(
                "SELECT m.status, m.content, r.error_code FROM messages m "
                "JOIN generation_runs r ON r.message_id = m.id WHERE m.status <> 'streaming'"
            )
        )
        assert row == ("stopped", "До пяти дней [c1]", "client_disconnected")
        assert chat.llm.cancelled
    finally:
        server.should_exit = True
        await asyncio.wait_for(serving, 10)


async def test_a_citation_opens_in_the_viewer_only_while_access_lasts(chat: Chat) -> None:
    [citation] = named(events(await send(chat)), "citation")
    route = citation["viewer_route"]
    api = (
        f"/api/v1/documents/{citation['document_id']}/versions/{citation['document_version_id']}"
        f"/chunks/{citation['chunk_id']}/view"
    )
    assert route.startswith(f"/viewer/documents/{citation['document_id']}/")
    opened = await chat.member.get(api)
    assert opened.status_code == 200, opened.text
    assert opened.json()["chunk"]["id"] == citation["chunk_id"]
    await grant_read(chat.env, chat.env.hr)  # access revoked: the citation in the history grants nothing
    problem(await chat.member.get(api), 404, "not_found")
