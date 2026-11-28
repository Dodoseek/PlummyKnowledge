"""No secret, token or private text reaches the logs, end to end (SECURITY_MODEL §14, Phase 12.5).

One scenario touches every kind of sensitive value: passwords, a failed login, an activation link, the
session cookie and the CSRF token, a provider key, a document's text, a search query, a chat question and
the answer. Every log record of the process, at DEBUG, is rendered by the production JSON formatter
(message and extra fields) and searched for each value.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import uuid4

import pytest

from plummy.core.logging import JsonFormatter
from plummy.infrastructure.storage.local import LocalFileStorage
from tests.integration.harness import running_auth_app
from tests.integration.rbac import CSRF_HEADER, activate, invite, login, token_from
from tests.integration.test_assistants_api import FAKE_KEY, Env, create, make_env
from tests.integration.test_chat_stream import ChatGateway, ScriptedLLM, clean_chat_keys, events, named
from tests.integration.test_conversations_api import grant_read
from tests.integration.test_document_upload import Env as DocumentEnv
from tests.integration.test_retrieval import TextEmbedder, indexed

pytestmark = pytest.mark.integration

PASSWORD = "canary-password-Zq81-never-logged"
WRONG_PASSWORD = "canary-wrong-password-Kx27"
DOCUMENT_TEXT = "canary-document-text-Lm44 is the vacation rule"
QUESTION = "canary-question-Pw93 about the vacation rule?"
ANSWER = "canary-answer-Rt55 per the rule [c1]."
SEARCH = "canary-search-Vb06 vacation"
PROVIDER_KEY = "sk-canary-provider-key-Hj7x0000000000000000"  # secret-scan: allow (fake test value)


@pytest.fixture
async def env(
    migrated_database_url: str, redis_url: str, clean_rate_limits: None, tmp_path: Path
) -> AsyncIterator[Env]:
    async with running_auth_app(migrated_database_url, redis_url, tmp_path / "bootstrap") as harness:
        env = await make_env(harness)
        yield env
        await clean_chat_keys(env)


def rendered(caplog: pytest.LogCaptureFixture) -> str:
    formatter = JsonFormatter()
    return "\n".join(formatter.format(record) for record in caplog.records)


async def test_no_sensitive_value_is_logged(
    env: Env, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    caplog.set_level(logging.DEBUG)
    harness = env.harness
    sensitive: list[str] = [
        PASSWORD,
        WRONG_PASSWORD,
        DOCUMENT_TEXT,
        QUESTION,
        ANSWER,
        SEARCH,
        PROVIDER_KEY,
        FAKE_KEY,
    ]

    # A user invited by link, activated with a password; a failed and a successful login.
    created = await invite(env.admin, "canary@example.com", roles=["member"])
    activation_token = token_from(created["activation"]["url"])
    sensitive.append(activation_token)
    async with harness.new_client() as browser:
        assert (await activate(browser, activation_token, PASSWORD)).status_code == 200
    member = harness.new_client()
    assert (await login(member, "canary@example.com", WRONG_PASSWORD)).status_code == 401
    assert (await login(member, "canary@example.com", PASSWORD)).status_code == 200
    sensitive.append(member.headers[CSRF_HEADER])
    sensitive.extend(cookie.value for cookie in member.cookies.jar if cookie.value)

    # A provider key, written once and stored encrypted.
    provider = await env.admin.post(
        "/api/v1/providers",
        json={
            "name": "Canary",
            "provider_type": "openai",
            "base_url": "https://1.1.1.1/v1",
            "api_key": PROVIDER_KEY,
        },
    )
    assert provider.status_code == 201, provider.text

    # A document indexed through the pipeline, searched and asked about in the chat.
    embedder = TextEmbedder()
    llm = ScriptedLLM(deltas=[ANSWER])
    gateway = ChatGateway(embedder, llm)
    resources = harness.resources
    monkeypatch.setattr(resources.knowledge.retrieve, "_gateway", gateway)
    monkeypatch.setattr(resources.knowledge.search._retrieve, "_gateway", gateway)
    monkeypatch.setattr(resources.chat.send, "_gateway", gateway)
    hr = DocumentEnv(harness, env.admin, env.hr, LocalFileStorage(harness.settings.storage_path))
    await indexed(hr, env.hr, "rules.md", f"# Vacation\n\n{DOCUMENT_TEXT}.\n".encode(), embedder)
    await grant_read(env, env.hr, created["user"]["id"])
    search = await member.post(f"/api/v1/knowledge-bases/{env.hr}/search", json={"query": SEARCH})
    assert search.status_code == 200, search.text
    assistant = await create(env, name="HR", knowledge_base_ids=[env.hr])
    stream = await member.post(
        "/api/v1/chat/stream",
        json={"assistant_id": assistant["id"], "content": QUESTION, "client_message_id": str(uuid4())},
    )
    assert named(events(stream), "message_complete")
    assert llm.requests, "the question reached the model"
    await member.aclose()

    text = rendered(caplog)
    assert len(caplog.records) > 10, "the scenario was logged at all (positive control)"
    leaked = [value for value in sensitive if value in text]
    assert leaked == []
