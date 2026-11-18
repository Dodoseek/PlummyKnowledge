"""Permission catalog and system roles (SECURITY_MODEL.md §3, ADR-012).

The database copy is seeded by migration ``0003``; a test keeps both in sync.
"""

from __future__ import annotations

from enum import StrEnum


class Permission(StrEnum):
    SYSTEM_SETTINGS_READ = "system.settings.read"
    SETTINGS_MANAGE = "settings.manage"
    USERS_READ = "users.read"
    USERS_MANAGE = "users.manage"
    GROUPS_READ = "groups.read"
    GROUPS_MANAGE = "groups.manage"
    ROLES_READ = "roles.read"
    ROLES_ASSIGN = "roles.assign"
    PROVIDERS_READ = "providers.read"
    PROVIDERS_MANAGE = "providers.manage"
    DATA_SOURCES_READ = "data_sources.read"
    DATA_SOURCES_MANAGE = "data_sources.manage"
    DATA_SOURCES_SYNC = "data_sources.sync"
    KNOWLEDGE_BASES_MANAGE = "knowledge_bases.manage"
    ASSISTANTS_MANAGE = "assistants.manage"
    JOBS_READ = "jobs.read"
    JOBS_MANAGE = "jobs.manage"
    AUDIT_READ = "audit.read"
    CHAT_USE = "chat.use"


class SystemRole(StrEnum):
    ADMIN = "admin"
    KNOWLEDGE_MANAGER = "knowledge_manager"
    MEMBER = "member"
    AUDITOR = "auditor"


SYSTEM_ROLE_PERMISSIONS: dict[SystemRole, frozenset[Permission]] = {
    SystemRole.ADMIN: frozenset(Permission),
    SystemRole.KNOWLEDGE_MANAGER: frozenset(
        {
            Permission.USERS_READ,
            Permission.GROUPS_READ,
            Permission.PROVIDERS_READ,
            Permission.DATA_SOURCES_READ,
            Permission.DATA_SOURCES_SYNC,
            Permission.KNOWLEDGE_BASES_MANAGE,
            Permission.JOBS_READ,
            Permission.JOBS_MANAGE,
            Permission.CHAT_USE,
        }
    ),
    SystemRole.MEMBER: frozenset({Permission.CHAT_USE}),
    SystemRole.AUDITOR: frozenset({Permission.AUDIT_READ}),
}
