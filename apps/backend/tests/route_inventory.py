"""The reviewed inventory of API routes and how each is authorized (SECURITY_MODEL §3.6, Phase 12.2).

Checked against the application by ``tests/unit/test_route_inventory.py`` and exercised for every role on real
PostgreSQL and Redis by ``tests/integration/test_rbac_matrix.py``. A new route needs an entry here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from plummy.domain.identity import Permission

P = Permission


class Access(StrEnum):
    # No session: the request carries its own proof (bootstrap token, activation token, OAuth state)
    # or needs none.
    PUBLIC = "public"
    # A session; the use case scopes the data to the session's user (own sessions, own conversations).
    OWN = "own"
    # A session and a permission, checked by the route and again by the use case.
    PERMISSION = "permission"
    # A session; the use case checks the access level to that knowledge base (read or manage),
    # 404 without access.
    KNOWLEDGE_BASE = "knowledge_base"
    # A session; ADR-005: assistants.manage, or chat.use and read access to every knowledge base
    # of the assistant.
    ASSISTANT = "assistant"


@dataclass(frozen=True, slots=True)
class Rule:
    access: Access
    # PERMISSION: what the route requires. Other kinds: what the use case requires, documentation only.
    permission: Permission | None = None
    # KNOWLEDGE_BASE: the level the use case requires ("read", "manage"; "listed" filters a list by access).
    level: str | None = None


def public() -> Rule:
    return Rule(Access.PUBLIC)


def own(permission: Permission | None = None) -> Rule:
    return Rule(Access.OWN, permission)


def perm(permission: Permission) -> Rule:
    return Rule(Access.PERMISSION, permission)


def kb(level: str) -> Rule:
    return Rule(Access.KNOWLEDGE_BASE, level=level)


def assistant(permission: Permission | None = None) -> Rule:
    return Rule(Access.ASSISTANT, permission)


V1 = "/api/v1"

ROUTES: dict[tuple[str, str], Rule] = {
    # Health: no data, used by orchestration.
    ("GET", "/health/live"): public(),
    ("GET", "/health/ready"): public(),
    # Initial setup: the bootstrap token (ADR-006).
    ("GET", "/setup/status"): public(),
    ("POST", "/setup/initialize"): public(),
    # Authentication (ADR-013, ADR-015).
    ("POST", "/auth/login"): public(),
    ("POST", "/auth/logout"): public(),
    ("POST", "/auth/activate"): public(),
    ("GET", "/auth/me"): own(),
    ("GET", "/auth/sessions"): own(),
    ("DELETE", "/auth/sessions/{session_id}"): own(),
    ("POST", "/auth/password/change"): own(),
    # Users, groups, roles.
    ("GET", "/users"): perm(P.USERS_READ),
    ("POST", "/users"): perm(P.USERS_MANAGE),
    ("GET", "/users/{user_id}"): perm(P.USERS_READ),
    ("PATCH", "/users/{user_id}"): perm(P.USERS_MANAGE),
    ("DELETE", "/users/{user_id}"): perm(P.USERS_MANAGE),
    ("PUT", "/users/{user_id}/roles"): perm(P.ROLES_ASSIGN),
    ("POST", "/users/{user_id}/sessions/revoke"): perm(P.USERS_MANAGE),
    ("POST", "/users/{user_id}/activation"): perm(P.USERS_MANAGE),
    ("GET", "/groups"): perm(P.GROUPS_READ),
    ("POST", "/groups"): perm(P.GROUPS_MANAGE),
    ("GET", "/groups/{group_id}"): perm(P.GROUPS_READ),
    ("PATCH", "/groups/{group_id}"): perm(P.GROUPS_MANAGE),
    ("DELETE", "/groups/{group_id}"): perm(P.GROUPS_MANAGE),
    ("PUT", "/groups/{group_id}/members"): perm(P.GROUPS_MANAGE),
    ("PUT", "/groups/{group_id}/roles"): perm(P.ROLES_ASSIGN),
    ("GET", "/roles"): perm(P.ROLES_READ),
    ("GET", "/roles/permissions"): perm(P.ROLES_READ),
    # AI providers, models, embedding profiles.
    ("GET", "/providers"): perm(P.PROVIDERS_READ),
    ("POST", "/providers"): perm(P.PROVIDERS_MANAGE),
    ("GET", "/providers/{provider_id}"): perm(P.PROVIDERS_READ),
    ("PATCH", "/providers/{provider_id}"): perm(P.PROVIDERS_MANAGE),
    ("DELETE", "/providers/{provider_id}"): perm(P.PROVIDERS_MANAGE),
    ("POST", "/providers/{provider_id}/test"): perm(P.PROVIDERS_MANAGE),
    ("GET", "/providers/{provider_id}/models"): perm(P.PROVIDERS_READ),
    ("POST", "/providers/{provider_id}/models"): perm(P.PROVIDERS_MANAGE),
    ("PATCH", "/providers/{provider_id}/models/{config_id}"): perm(P.PROVIDERS_MANAGE),
    ("DELETE", "/providers/{provider_id}/models/{config_id}"): perm(P.PROVIDERS_MANAGE),
    ("GET", "/embedding-profiles"): perm(P.PROVIDERS_READ),
    ("POST", "/embedding-profiles"): perm(P.PROVIDERS_MANAGE),
    ("GET", "/embedding-profiles/{profile_id}"): perm(P.PROVIDERS_READ),
    ("PATCH", "/embedding-profiles/{profile_id}"): perm(P.PROVIDERS_MANAGE),
    ("POST", "/embedding-profiles/{profile_id}/test"): perm(P.PROVIDERS_MANAGE),
    # Knowledge bases and documents: the access level to the knowledge base (SECURITY_MODEL §3.4).
    ("GET", "/knowledge-bases"): kb("listed"),
    ("POST", "/knowledge-bases"): perm(P.KNOWLEDGE_BASES_MANAGE),
    ("GET", "/knowledge-bases/{kb_id}"): kb("read"),
    ("PATCH", "/knowledge-bases/{kb_id}"): kb("manage"),
    ("DELETE", "/knowledge-bases/{kb_id}"): perm(P.KNOWLEDGE_BASES_MANAGE),
    ("GET", "/knowledge-bases/{kb_id}/access"): kb("manage"),
    ("PUT", "/knowledge-bases/{kb_id}/access"): kb("manage"),
    ("POST", "/knowledge-bases/{kb_id}/reindex"): kb("manage"),
    ("POST", "/knowledge-bases/{kb_id}/search"): kb("read"),
    ("GET", "/documents"): kb("read"),
    ("POST", "/documents"): kb("manage"),
    ("GET", "/documents/{document_id}"): kb("read"),
    ("GET", "/documents/{document_id}/versions"): kb("read"),
    ("GET", "/documents/{document_id}/versions/{version_id}/chunks/{chunk_id}/view"): kb("read"),
    ("DELETE", "/documents/{document_id}"): kb("manage"),
    ("POST", "/documents/{document_id}/reindex"): kb("manage"),
    # External sources (ADR-020, ADR-021). The OAuth callback is proven by the state bound to the session.
    ("GET", "/data-sources"): perm(P.DATA_SOURCES_READ),
    ("POST", "/data-sources"): perm(P.DATA_SOURCES_MANAGE),
    ("GET", "/data-sources/oauth/callback"): public(),
    ("GET", "/data-sources/{source_id}"): perm(P.DATA_SOURCES_READ),
    ("PATCH", "/data-sources/{source_id}"): perm(P.DATA_SOURCES_MANAGE),
    ("DELETE", "/data-sources/{source_id}"): perm(P.DATA_SOURCES_MANAGE),
    ("POST", "/data-sources/{source_id}/oauth/start"): perm(P.DATA_SOURCES_MANAGE),
    ("POST", "/data-sources/{source_id}/disconnect"): perm(P.DATA_SOURCES_MANAGE),
    ("POST", "/data-sources/{source_id}/sync"): perm(P.DATA_SOURCES_SYNC),
    ("POST", "/data-sources/{source_id}/validate"): perm(P.DATA_SOURCES_MANAGE),
    ("GET", "/data-sources/{source_id}/browse"): perm(P.DATA_SOURCES_MANAGE),
    # Assistants (ADR-005).
    ("GET", "/assistants"): assistant(),
    ("POST", "/assistants"): assistant(P.ASSISTANTS_MANAGE),
    ("GET", "/assistants/{assistant_id}"): assistant(),
    ("PATCH", "/assistants/{assistant_id}"): assistant(P.ASSISTANTS_MANAGE),
    ("DELETE", "/assistants/{assistant_id}"): assistant(P.ASSISTANTS_MANAGE),
    # Conversations and chat: the owner's, with chat.use and the assistant still available.
    ("GET", "/conversations"): own(P.CHAT_USE),
    ("POST", "/conversations"): own(P.CHAT_USE),
    ("GET", "/conversations/{conversation_id}"): own(P.CHAT_USE),
    ("PATCH", "/conversations/{conversation_id}"): own(P.CHAT_USE),
    ("DELETE", "/conversations/{conversation_id}"): own(P.CHAT_USE),
    ("GET", "/conversations/{conversation_id}/messages"): own(P.CHAT_USE),
    ("POST", "/chat/stream"): own(P.CHAT_USE),
    ("POST", "/chat/runs/{run_id}/stop"): own(P.CHAT_USE),
    # Jobs.
    ("GET", "/audit/events"): perm(P.AUDIT_READ),
    ("GET", "/audit/events/{event_id}"): perm(P.AUDIT_READ),
    ("GET", "/jobs"): perm(P.JOBS_READ),
    ("GET", "/jobs/{job_type}/{job_id}"): perm(P.JOBS_READ),
    ("POST", "/jobs/{job_type}/{job_id}/retry"): perm(P.JOBS_MANAGE),
    ("POST", "/jobs/{job_type}/{job_id}/cancel"): perm(P.JOBS_MANAGE),
    # Instance Settings are read-only through the API (ADR-006).
    ("GET", "/system/settings"): perm(P.SYSTEM_SETTINGS_READ),
}

# Permissions no route requires yet, with the reason; anything else unused is a mistake in the catalog.
RESERVED: dict[Permission, str] = {
    P.SETTINGS_MANAGE: "Application Settings are not implemented yet",
}
