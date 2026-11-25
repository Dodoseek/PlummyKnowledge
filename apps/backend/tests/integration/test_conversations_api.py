"""``/api/v1/conversations`` on real PostgreSQL and Redis (API_CONTRACTS.md §2.13, DATABASE_SCHEMA.md §6).

A conversation is private to its owner; starting one follows ADR-005. Messages are written by the chat stream
(8.3); here they are inserted directly to test reading the history.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx
import pytest

from tests.integration.harness import running_auth_app
from tests.integration.rbac import audit_actions
from tests.integration.test_assistants_api import KBS, Env, create, make_env, signed_in
from tests.integration.test_assistants_api import URL as ASSISTANTS

pytestmark = pytest.mark.integration

URL = "/api/v1/conversations"


@dataclass
class Chat:
    env: Env
    assistant: str
    member: httpx.AsyncClient
    member_id: str
    other: httpx.AsyncClient


@pytest.fixture
async def env(
    migrated_database_url: str, redis_url: str, clean_rate_limits: None, tmp_path: Path
) -> AsyncIterator[Env]:
    async with running_auth_app(migrated_database_url, redis_url, tmp_path / "bootstrap") as harness:
        yield await make_env(harness)


async def grant_read(env: Env, kb_id: str, *user_ids: str) -> None:
    grants = [{"principal_type": "user", "principal_id": uid, "access_level": "read"} for uid in user_ids]
    response = await env.admin.put(f"{KBS}/{kb_id}/access", json={"grants": grants})
    assert response.status_code == 200, response.text


@pytest.fixture
async def chat(env: Env) -> Chat:
    assistant = await create(env, name="HR", knowledge_base_ids=[env.hr])
    member, member_id = await signed_in(env, "member@example.com", ["member"])
    other, other_id = await signed_in(env, "other@example.com", ["member"])
    await grant_read(env, env.hr, member_id, other_id)
    return Chat(env, assistant["id"], member, member_id, other)


async def start(client: httpx.AsyncClient, assistant_id: str, **extra: Any) -> dict[str, Any]:
    response = await client.post(URL, json={"assistant_id": assistant_id, **extra})
    assert response.status_code == 201, response.text
    created: dict[str, Any] = response.json()
    return created


def problem(response: httpx.Response, status: int, code: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    payload: dict[str, Any] = response.json()
    assert payload["code"] == code, payload
    return payload


async def add_message(
    env: Env, conversation_id: str, sequence: int, role: str, content: str, *, status: str = "completed"
) -> str:
    message_id = str(uuid4())
    await env.harness.sql(
        "INSERT INTO messages (id, conversation_id, sequence, role, content, status, client_message_id) "
        "VALUES (:id, :conversation, :sequence, :role, :content, :status, "
        "CASE WHEN :role = 'user' THEN gen_random_uuid() END)",
        id=message_id,
        conversation=conversation_id,
        sequence=sequence,
        role=role,
        content=content,
        status=status,
    )
    return message_id


# --- create -------------------------------------------------------------------------------------------------


async def test_start_a_conversation(chat: Chat) -> None:
    created = await start(chat.member, chat.assistant)
    assert created["title"] == "New conversation"
    assert created["assistant"] == {"id": chat.assistant, "name": "HR", "available": True}
    assert created["last_message_at"] == created["created_at"]
    titled = await start(chat.member, chat.assistant, title="  Vacation \n  questions ")
    assert titled["title"] == "Vacation questions"


@pytest.mark.parametrize("title", ["   ", "a\u0000b", "x" * 201])
async def test_an_invalid_title_is_refused(chat: Chat, title: str) -> None:
    response = await chat.member.post(URL, json={"assistant_id": chat.assistant, "title": title})
    payload = problem(response, 422, "validation_error")
    assert payload["errors"][0]["field"] == "body.title"


async def test_only_a_usable_assistant_starts_a_conversation(chat: Chat) -> None:
    env = chat.env
    it_only = await create(env, name="IT", knowledge_base_ids=[env.it])
    off = await create(env, name="Off", knowledge_base_ids=[env.hr], status="disabled")
    for assistant_id in (it_only["id"], off["id"], str(uuid4())):
        problem(await chat.member.post(URL, json={"assistant_id": assistant_id}), 404, "not_found")
    # assistants.manage may preview an assistant without knowledge bases, but not chat with a disabled one.
    empty = await create(env, name="Empty", knowledge_base_ids=[])
    assert (await env.admin.post(URL, json={"assistant_id": empty["id"]})).status_code == 201
    problem(await env.admin.post(URL, json={"assistant_id": off["id"]}), 404, "not_found")
    assert await env.harness.sql("SELECT count(*) FROM conversations") == [(1,)]


async def test_chat_use_is_required(chat: Chat) -> None:
    reader, reader_id = await signed_in(chat.env, "reader@example.com", [])
    await grant_read(chat.env, chat.env.hr, chat.member_id, reader_id)
    problem(await reader.post(URL, json={"assistant_id": chat.assistant}), 403, "permission_denied")
    problem(await reader.get(URL), 403, "permission_denied")


# --- list, read, rename, delete -----------------------------------------------------------------------------


async def test_the_list_shows_my_conversations_only_newest_first(chat: Chat) -> None:
    first = await start(chat.member, chat.assistant, title="First")
    second = await start(chat.member, chat.assistant, title="Second")
    await start(chat.other, chat.assistant, title="Not mine")
    await chat.env.harness.sql(
        "UPDATE conversations SET last_message_at = now() + interval '1 hour' WHERE id = :id", id=first["id"]
    )
    listed = (await chat.member.get(URL)).json()
    assert [item["title"] for item in listed["items"]] == ["First", "Second"]
    by_created = (await chat.member.get(URL, params={"sort": "-created_at"})).json()
    assert [item["id"] for item in by_created["items"]] == [second["id"], first["id"]]
    paged = (await chat.member.get(URL, params={"limit": 1})).json()
    assert paged["has_more"] is True
    rest = (await chat.member.get(URL, params={"limit": 1, "cursor": paged["next_cursor"]})).json()
    assert [item["title"] for item in rest["items"]] == ["Second"]
    problem(await chat.member.get(URL, params={"cursor": "nonsense"}), 422, "invalid_query_parameter")
    problem(await chat.member.get(URL, params={"user_id": chat.member_id}), 422, "invalid_query_parameter")


async def test_filter_by_assistant(chat: Chat) -> None:
    env = chat.env
    both = await create(env, name="Both", knowledge_base_ids=[env.hr])
    await start(chat.member, chat.assistant, title="A")
    await start(chat.member, both["id"], title="B")
    listed = (await chat.member.get(URL, params={"assistant_id": both["id"]})).json()
    assert [item["title"] for item in listed["items"]] == ["B"]


async def test_someone_elses_conversation_does_not_exist(chat: Chat) -> None:
    mine = await start(chat.member, chat.assistant)
    url = f"{URL}/{mine['id']}"
    for response in (
        await chat.other.get(url),
        await chat.other.patch(url, json={"title": "Mine now"}),
        await chat.other.delete(url),
        await chat.other.get(f"{url}/messages"),
        # Not even assistants.manage or an administrator can read it.
        await chat.env.admin.get(url),
        await chat.env.admin.get(f"{url}/messages"),
    ):
        problem(response, 404, "not_found")
    assert (await chat.member.get(url)).json()["title"] == "New conversation"


async def test_rename(chat: Chat) -> None:
    created = await start(chat.member, chat.assistant)
    url = f"{URL}/{created['id']}"
    renamed = (await chat.member.patch(url, json={"title": " Leave  policy "})).json()
    assert renamed["title"] == "Leave policy"
    assert renamed["updated_at"] > created["updated_at"]
    problem(await chat.member.patch(url, json={"title": "\t"}), 422, "validation_error")
    extra = await chat.member.patch(url, json={"title": "x", "user_id": chat.member_id})
    problem(extra, 422, "validation_error")


async def test_delete_hides_at_once_and_is_audited(chat: Chat) -> None:
    created = await start(chat.member, chat.assistant)
    await add_message(chat.env, created["id"], 1, "user", "A private question")
    url = f"{URL}/{created['id']}"
    assert (await chat.member.delete(url)).status_code == 204
    problem(await chat.member.get(url), 404, "not_found")
    problem(await chat.member.get(f"{url}/messages"), 404, "not_found")
    problem(await chat.member.delete(url), 404, "not_found")
    assert (await chat.member.get(URL)).json()["items"] == []
    assert "conversation.deleted" in await audit_actions(chat.env.harness)
    rows = await chat.env.harness.sql(
        "SELECT details::text FROM audit_logs WHERE action = 'conversation.deleted'"
    )
    assert "private question" not in rows[0][0]


# --- history ------------------------------------------------------------------------------------------------


async def test_messages_newest_first_with_citations(chat: Chat) -> None:
    created = await start(chat.member, chat.assistant)
    env = chat.env
    await add_message(env, created["id"], 1, "user", "How many vacation days?")
    answer = await add_message(env, created["id"], 2, "assistant", "28 days [c1].")
    await add_message(env, created["id"], 3, "user", "And sick leave?")
    await add_message(env, created["id"], 4, "assistant", "Partial", status="stopped")
    ids = [str(uuid4()) for _ in range(3)]
    await env.harness.sql(
        "INSERT INTO message_citations (message_id, citation_key, ordinal, document_id, document_version_id, "
        "chunk_id, filename, page, section, snippet, viewer_route) VALUES (:message, 'c1', 1, :d, :v, :c, "
        "'Policy.pdf', 3, 'Leave', '28 calendar days', '/viewer/x')",
        message=answer,
        d=ids[0],
        v=ids[1],
        c=ids[2],
    )
    url = f"{URL}/{created['id']}/messages"
    newest = (await chat.member.get(url, params={"limit": 2})).json()
    assert [(m["sequence"], m["role"], m["status"]) for m in newest["items"]] == [
        (4, "assistant", "stopped"),
        (3, "user", "completed"),
    ]
    older = (await chat.member.get(url, params={"limit": 2, "cursor": newest["next_cursor"]})).json()
    assert [m["sequence"] for m in older["items"]] == [2, 1]
    assert older["has_more"] is False
    citation = older["items"][0]["citations"][0]
    assert citation == {
        "citation_id": "c1",
        "document_id": ids[0],
        "document_version_id": ids[1],
        "chunk_id": ids[2],
        "filename": "Policy.pdf",
        "page": 3,
        "section": "Leave",
        "sheet": None,
        "row": None,
        "snippet": "28 calendar days",
        "viewer_route": "/viewer/x",
    }
    oldest_first = (await chat.member.get(url, params={"sort": "sequence"})).json()
    assert [m["sequence"] for m in oldest_first["items"]] == [1, 2, 3, 4]
    # A cursor of one sort is not valid for the other.
    problem(
        await chat.member.get(url, params={"sort": "sequence", "cursor": newest["next_cursor"]}),
        422,
        "invalid_query_parameter",
    )


async def test_history_stays_readable_after_losing_the_assistant(chat: Chat) -> None:
    created = await start(chat.member, chat.assistant)
    await add_message(chat.env, created["id"], 1, "user", "Question")
    await grant_read(chat.env, chat.env.hr)  # nobody has access any more
    url = f"{URL}/{created['id']}"
    fetched = (await chat.member.get(url)).json()
    assert fetched["assistant"] == {"id": chat.assistant, "name": "HR", "available": False}
    assert [m["content"] for m in (await chat.member.get(f"{url}/messages")).json()["items"]] == ["Question"]
    listed = (await chat.member.get(URL)).json()["items"]
    assert listed[0]["assistant"]["available"] is False


async def test_a_deleted_assistant_keeps_its_name_in_the_history(chat: Chat) -> None:
    created = await start(chat.member, chat.assistant)
    assert (await chat.env.admin.delete(f"{ASSISTANTS}/{chat.assistant}")).status_code == 204
    fetched = (await chat.member.get(f"{URL}/{created['id']}")).json()
    assert fetched["assistant"] == {"id": chat.assistant, "name": "HR", "available": False}
