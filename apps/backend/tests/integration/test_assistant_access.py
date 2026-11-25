"""Who can see and use an assistant (ADR-005, SECURITY_MODEL.md §6.3, SC-01) on real PostgreSQL and Redis.

The rule: ``read`` or better on **every** knowledge base of the assistant (direct, through a group or
``knowledge_bases.manage``), ``chat.use``, and the assistant active and with knowledge bases. The chat list
filters in SQL, the card checks through the access policy; every test asserts that both agree. Access is
evaluated on each request, so a revocation hides the assistant at once, without a new sign-in.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.integration.harness import running_auth_app
from tests.integration.rbac import login
from tests.integration.test_assistants_api import KBS, URL, Env, create, create_kb, make_env, signed_in

pytestmark = pytest.mark.integration

GROUPS = "/api/v1/groups"


@dataclass
class Matrix:
    env: Env
    both: str  # HR and IT
    hr_only: str
    empty: str  # no knowledge bases
    disabled: str  # HR and IT, disabled


@pytest.fixture
async def env(
    migrated_database_url: str, redis_url: str, clean_rate_limits: None, tmp_path: Path
) -> AsyncIterator[Env]:
    async with running_auth_app(migrated_database_url, redis_url, tmp_path / "bootstrap") as harness:
        yield await make_env(harness)


@pytest.fixture
async def matrix(env: Env) -> Matrix:
    both = await create(env, name="Both", knowledge_base_ids=[env.hr, env.it])
    hr_only = await create(env, name="HR only", knowledge_base_ids=[env.hr])
    empty = await create(env, name="Empty", knowledge_base_ids=[])
    disabled = await create(env, name="Off", knowledge_base_ids=[env.hr, env.it], status="disabled")
    return Matrix(env, both["id"], hr_only["id"], empty["id"], disabled["id"])


def grant(principal_type: str, principal_id: str, level: str = "read") -> dict[str, str]:
    return {"principal_type": principal_type, "principal_id": principal_id, "access_level": level}


async def set_access(env: Env, kb_id: str, *grants: dict[str, str]) -> None:
    response = await env.admin.put(f"{KBS}/{kb_id}/access", json={"grants": list(grants)})
    assert response.status_code == 200, response.text


async def new_group(env: Env, name: str, *user_ids: str) -> str:
    created = await env.admin.post(GROUPS, json={"name": name})
    assert created.status_code == 201, created.text
    group_id: str = created.json()["id"]
    await set_members(env, group_id, *user_ids)
    return group_id


async def set_members(env: Env, group_id: str, *user_ids: str) -> None:
    response = await env.admin.put(f"{GROUPS}/{group_id}/members", json={"user_ids": list(user_ids)})
    assert response.status_code == 200, response.text


async def listed(client: httpx.AsyncClient) -> set[str]:
    response = await client.get(URL)
    assert response.status_code == 200, response.text
    items: list[dict[str, Any]] = response.json()["items"]
    return {item["id"] for item in items}


async def usable(client: httpx.AsyncClient, matrix: Matrix) -> set[str]:
    """The assistants a user without assistants.manage sees: the list and the cards must agree."""
    ids = (matrix.both, matrix.hr_only, matrix.empty, matrix.disabled)
    cards: set[str] = set()
    for assistant_id in ids:
        response = await client.get(f"{URL}/{assistant_id}")
        if response.status_code == 200:
            card = response.json()
            assert card["availability"] == {"available": True, "reason": None}
            assert card["instructions"] is None  # configuration stays with assistants.manage
            cards.add(assistant_id)
        else:
            # Not usable looks exactly like not existing (ADR-005 §5).
            assert response.status_code == 404, response.text
            assert response.json()["code"] == "not_found"
    assert await listed(client) == cards
    return cards


# --- the matrix --------------------------------------------------------------------------------------------


async def test_direct_read_on_every_knowledge_base(matrix: Matrix) -> None:
    env = matrix.env
    client, user_id = await signed_in(env, "direct@example.com", ["member"])
    await set_access(env, env.hr, grant("user", user_id))
    await set_access(env, env.it, grant("user", user_id, "manage"))  # manage includes read
    assert await usable(client, matrix) == {matrix.both, matrix.hr_only}


async def test_read_through_a_group(matrix: Matrix) -> None:
    env = matrix.env
    client, user_id = await signed_in(env, "grouped@example.com", ["member"])
    group_id = await new_group(env, "Staff", user_id)
    await set_access(env, env.hr, grant("group", group_id))
    await set_access(env, env.it, grant("group", group_id))
    assert await usable(client, matrix) == {matrix.both, matrix.hr_only}


async def test_direct_and_group_access_add_up(matrix: Matrix) -> None:
    env = matrix.env
    client, user_id = await signed_in(env, "mixed@example.com", ["member"])
    group_id = await new_group(env, "IT team", user_id)
    await set_access(env, env.hr, grant("user", user_id))
    await set_access(env, env.it, grant("group", group_id))
    assert await usable(client, matrix) == {matrix.both, matrix.hr_only}


async def test_sc01_access_to_one_of_two_knowledge_bases_is_not_enough(matrix: Matrix) -> None:
    """SC-01: access to HR but not IT; the assistant on both is neither listed nor readable."""
    env = matrix.env
    client, user_id = await signed_in(env, "partial@example.com", ["member"])
    await set_access(env, env.hr, grant("user", user_id))
    assert await usable(client, matrix) == {matrix.hr_only}
    response = await client.get(f"{URL}/{matrix.both}")
    assert response.status_code == 404
    # Nothing about its knowledge bases leaks.
    assert env.hr not in response.text
    assert env.it not in response.text


async def test_without_any_access(matrix: Matrix) -> None:
    client, _ = await signed_in(matrix.env, "none@example.com", ["member"])
    assert await usable(client, matrix) == set()


async def test_knowledge_bases_manage_reads_every_knowledge_base(matrix: Matrix) -> None:
    client, _ = await signed_in(matrix.env, "km@example.com", ["knowledge_manager"])
    assert await usable(client, matrix) == {matrix.both, matrix.hr_only}


async def test_knowledge_base_access_without_chat_use(matrix: Matrix) -> None:
    env = matrix.env
    client, user_id = await signed_in(env, "reader@example.com", [])
    await set_access(env, env.hr, grant("user", user_id))
    await set_access(env, env.it, grant("user", user_id))
    response = await client.get(URL)
    assert response.status_code == 403
    assert response.json()["code"] == "permission_denied"
    for assistant_id in (matrix.both, matrix.hr_only):
        assert (await client.get(f"{URL}/{assistant_id}")).status_code == 404


async def test_assistants_manage_sees_everything_with_its_availability(matrix: Matrix) -> None:
    response = await matrix.env.admin.get(URL)
    availability = {item["id"]: item["availability"] for item in response.json()["items"]}
    assert availability == {
        matrix.both: {"available": True, "reason": None},
        matrix.hr_only: {"available": True, "reason": None},
        matrix.empty: {"available": True, "reason": None},  # preview while it is set up (ADR-005 §4)
        matrix.disabled: {"available": False, "reason": "disabled"},
    }


async def test_a_manager_without_knowledge_base_access_sees_why(env: Env) -> None:
    # assistants.manage and chat.use, but no access to IT.
    _, builder_id = await signed_in(env, "builder@example.com", [])
    await env.harness.sql(
        "INSERT INTO roles (id, key, name, description) VALUES (gen_random_uuid(), 'builder', 'Builder', '')"
    )
    await env.harness.sql(
        "INSERT INTO role_permissions (role_id, permission_id) SELECT r.id, p.id FROM roles r, permissions p "
        "WHERE r.key = 'builder' AND p.key IN ('assistants.manage', 'chat.use')"
    )
    await env.harness.sql(
        "INSERT INTO user_roles (user_id, role_id) SELECT :user, id FROM roles WHERE key = 'builder'",
        user=builder_id,
    )
    builder = env.harness.new_client()
    assert (await login(builder, "builder@example.com")).status_code == 200
    await set_access(env, env.hr, grant("user", builder_id))
    both = await create(env, name="Both", knowledge_base_ids=[env.hr, env.it])
    card = (await builder.get(f"{URL}/{both['id']}")).json()
    assert card["availability"] == {"available": False, "reason": "missing_kb_access"}
    assert card["instructions"] is not None  # configuration is visible to assistants.manage
    assert [item["availability"]["reason"] for item in (await builder.get(URL)).json()["items"]] == [
        "missing_kb_access"
    ]


# --- revocation takes effect on the next request ----------------------------------------------------------


async def test_revoking_a_direct_grant_hides_the_assistant(matrix: Matrix) -> None:
    env = matrix.env
    client, user_id = await signed_in(env, "direct@example.com", ["member"])
    await set_access(env, env.hr, grant("user", user_id))
    assert await usable(client, matrix) == {matrix.hr_only}
    await set_access(env, env.hr)
    assert await usable(client, matrix) == set()


async def test_revoking_a_group_grant_hides_the_assistant(matrix: Matrix) -> None:
    env = matrix.env
    client, user_id = await signed_in(env, "grouped@example.com", ["member"])
    group_id = await new_group(env, "Staff", user_id)
    await set_access(env, env.hr, grant("group", group_id))
    assert await usable(client, matrix) == {matrix.hr_only}
    await set_access(env, env.hr)
    assert await usable(client, matrix) == set()


async def test_leaving_the_group_hides_the_assistant(matrix: Matrix) -> None:
    env = matrix.env
    client, user_id = await signed_in(env, "grouped@example.com", ["member"])
    group_id = await new_group(env, "Staff", user_id)
    await set_access(env, env.hr, grant("group", group_id))
    await set_access(env, env.it, grant("group", group_id))
    assert await usable(client, matrix) == {matrix.both, matrix.hr_only}
    await set_members(env, group_id)
    assert await usable(client, matrix) == set()


async def test_deleting_the_group_hides_the_assistant(matrix: Matrix) -> None:
    env = matrix.env
    client, user_id = await signed_in(env, "grouped@example.com", ["member"])
    group_id = await new_group(env, "Staff", user_id)
    await set_access(env, env.hr, grant("group", group_id))
    assert await usable(client, matrix) == {matrix.hr_only}
    assert (await env.admin.delete(f"{GROUPS}/{group_id}")).status_code == 204
    assert await usable(client, matrix) == set()


async def test_losing_knowledge_bases_manage_hides_the_assistant(matrix: Matrix) -> None:
    env = matrix.env
    client, user_id = await signed_in(env, "km@example.com", ["knowledge_manager"])
    assert await usable(client, matrix) == {matrix.both, matrix.hr_only}
    response = await env.admin.put(f"/api/v1/users/{user_id}/roles", json={"roles": ["member"]})
    assert response.status_code == 200, response.text
    assert await usable(client, matrix) == set()


async def test_disabling_the_assistant_hides_it(matrix: Matrix) -> None:
    env = matrix.env
    client, _ = await signed_in(env, "km@example.com", ["knowledge_manager"])
    assert matrix.hr_only in await usable(client, matrix)
    response = await env.admin.patch(f"{URL}/{matrix.hr_only}", json={"status": "disabled"})
    assert response.status_code == 200, response.text
    assert await usable(client, matrix) == {matrix.both}


async def test_deleting_its_only_knowledge_base_hides_the_assistant(matrix: Matrix) -> None:
    env = matrix.env
    client, user_id = await signed_in(env, "direct@example.com", ["member"])
    await set_access(env, env.hr, grant("user", user_id))
    assert await usable(client, matrix) == {matrix.hr_only}
    assert (await env.admin.delete(f"{KBS}/{env.hr}")).status_code == 204
    # No knowledge base left: only assistants.manage can still open it (to reconfigure it).
    assert await usable(client, matrix) == set()
    card = (await env.admin.get(f"{URL}/{matrix.hr_only}")).json()
    assert card["knowledge_base_ids"] == []


async def test_a_deleted_knowledge_base_no_longer_counts(matrix: Matrix) -> None:
    """The assistant keeps only its remaining knowledge bases, so access to those is enough afterwards."""
    env = matrix.env
    client, user_id = await signed_in(env, "partial@example.com", ["member"])
    await set_access(env, env.hr, grant("user", user_id))
    assert await usable(client, matrix) == {matrix.hr_only}
    assert (await env.admin.delete(f"{KBS}/{env.it}")).status_code == 204
    assert await usable(client, matrix) == {matrix.both, matrix.hr_only}


async def test_adding_a_knowledge_base_the_user_cannot_read_hides_the_assistant(matrix: Matrix) -> None:
    env = matrix.env
    client, user_id = await signed_in(env, "partial@example.com", ["member"])
    await set_access(env, env.hr, grant("user", user_id))
    third = await create_kb(env.admin, "Finance", env.profile_id)
    response = await env.admin.patch(f"{URL}/{matrix.hr_only}", json={"knowledge_base_ids": [env.hr, third]})
    assert response.status_code == 200, response.text
    assert await usable(client, matrix) == set()
