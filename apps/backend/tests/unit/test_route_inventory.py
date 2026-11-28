"""Every API route is in the reviewed inventory and its dependencies match it (SECURITY_MODEL §3.6).

A route that appears in the application without an entry in ``tests/route_inventory.py``, or whose
dependencies stop matching its entry, fails this test: a new endpoint cannot land without someone deciding
how it is protected. The behaviour behind each kind is tested on real PostgreSQL and Redis in
``tests/integration/test_rbac_matrix.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, iter_route_contexts

from plummy.api.security import RequirePermission, get_authenticated, verify_origin
from plummy.domain.identity import Permission
from plummy.domain.identity.permissions import SYSTEM_ROLE_PERMISSIONS, SystemRole
from tests.conftest import make_settings
from tests.route_inventory import RESERVED, ROUTES, V1, Access

P = Permission


@dataclass(frozen=True, slots=True)
class Found:
    method: str
    path: str
    dependencies: frozenset[object]
    permissions: frozenset[Permission]


def _calls(dependant: Dependant) -> Iterator[object]:
    for dependency in dependant.dependencies:
        yield dependency.call
        yield from _calls(dependency)


def _routes() -> list[Found]:
    from plummy.main import create_app

    app = create_app(make_settings())
    found: list[Found] = []
    for context in iter_route_contexts(app.routes):
        if not isinstance(context.original_route, APIRoute):
            continue  # the OpenAPI document and Swagger UI
        calls = list(_calls(context.dependant))
        permissions = frozenset(
            p for call in calls if isinstance(call, RequirePermission) for p in call.permissions
        )
        for method in sorted((context.methods or set()) - {"HEAD"}):
            assert context.path is not None
            found.append(Found(method, context.path, frozenset(calls), permissions))
    return found


@pytest.fixture(scope="module")
def routes() -> list[Found]:
    return _routes()


def _key(route: Found) -> tuple[str, str]:
    assert route.path.startswith(V1), route.path
    return route.method, route.path.removeprefix(V1)


def test_every_route_is_in_the_inventory_and_nothing_else(routes: list[Found]) -> None:
    keys = [_key(route) for route in routes]
    assert len(keys) == len(set(keys))
    missing = sorted(set(keys) - ROUTES.keys())
    stale = sorted(ROUTES.keys() - set(keys))
    assert not missing, f"routes without an entry in ROUTES (decide how they are authorized): {missing}"
    assert not stale, f"entries for routes that no longer exist: {stale}"


def test_only_public_routes_work_without_a_session(routes: list[Found]) -> None:
    for route in routes:
        rule = ROUTES[_key(route)]
        authenticated = get_authenticated in route.dependencies
        assert authenticated is (rule.access is not Access.PUBLIC), (route.method, route.path)


def test_permission_routes_require_exactly_their_permission(routes: list[Found]) -> None:
    for route in routes:
        rule = ROUTES[_key(route)]
        expected = {rule.permission} if rule.access is Access.PERMISSION else set()
        assert route.permissions == expected, (route.method, route.path)


def test_every_route_checks_the_origin(routes: list[Found]) -> None:
    # Unsafe methods additionally need the CSRF token, which get_authenticated checks (ADR-013).
    for route in routes:
        assert verify_origin in route.dependencies, (route.method, route.path)


def test_rules_are_well_formed() -> None:
    for key, rule in ROUTES.items():
        assert (rule.access is Access.PERMISSION) <= (rule.permission is not None), key
        assert (rule.level is not None) is (rule.access is Access.KNOWLEDGE_BASE), key
        assert rule.level in {None, "listed", "read", "manage"}, key
        assert rule.access is not Access.PUBLIC or rule.permission is None, key


def test_every_permission_is_used_or_reserved_with_a_reason() -> None:
    used = {rule.permission for rule in ROUTES.values() if rule.permission is not None}
    # Required by use cases on routes of another kind (SECURITY_MODEL §3.4, §6.3).
    used |= {P.KNOWLEDGE_BASES_MANAGE, P.ASSISTANTS_MANAGE, P.CHAT_USE}
    assert set(Permission) == used | RESERVED.keys()
    assert not used & RESERVED.keys(), "a reserved permission is in use: remove it from RESERVED"


# SECURITY_MODEL §3.3, written out rather than derived from the code it checks.
ROLE_MATRIX: dict[SystemRole, set[Permission]] = {
    SystemRole.ADMIN: set(Permission),
    SystemRole.KNOWLEDGE_MANAGER: {
        P.USERS_READ,
        P.GROUPS_READ,
        P.PROVIDERS_READ,
        P.DATA_SOURCES_READ,
        P.DATA_SOURCES_SYNC,
        P.KNOWLEDGE_BASES_MANAGE,
        P.JOBS_READ,
        P.JOBS_MANAGE,
        P.CHAT_USE,
    },
    SystemRole.MEMBER: {P.CHAT_USE},
    SystemRole.AUDITOR: {P.AUDIT_READ},
}


def test_system_roles_match_the_documented_matrix() -> None:
    assert {role: set(permissions) for role, permissions in SYSTEM_ROLE_PERMISSIONS.items()} == ROLE_MATRIX
