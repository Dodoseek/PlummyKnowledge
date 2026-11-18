"""Identity domain: users, sessions, roles, permissions, password and email rules (no I/O, no frameworks)."""

from plummy.domain.identity.emails import InvalidEmailError, canonical_login_email, normalize_email
from plummy.domain.identity.groups import (
    ROLES_NOT_ASSIGNABLE_TO_GROUPS,
    Group,
    InvalidGroupDescriptionError,
    InvalidGroupNameError,
    normalize_group_description,
    normalize_group_name,
)
from plummy.domain.identity.models import (
    InstanceState,
    InvalidDisplayNameError,
    SessionRevokeReason,
    SessionState,
    User,
    UserSession,
    UserStatus,
    normalize_display_name,
)
from plummy.domain.identity.passwords import (
    MAX_PASSWORD_LENGTH,
    MIN_PASSWORD_LENGTH,
    PasswordPolicyError,
    check_password_policy,
)
from plummy.domain.identity.permissions import SYSTEM_ROLE_PERMISSIONS, Permission, SystemRole

__all__ = [
    "MAX_PASSWORD_LENGTH",
    "MIN_PASSWORD_LENGTH",
    "ROLES_NOT_ASSIGNABLE_TO_GROUPS",
    "SYSTEM_ROLE_PERMISSIONS",
    "Group",
    "InstanceState",
    "InvalidDisplayNameError",
    "InvalidEmailError",
    "InvalidGroupDescriptionError",
    "InvalidGroupNameError",
    "PasswordPolicyError",
    "Permission",
    "SessionRevokeReason",
    "SessionState",
    "SystemRole",
    "User",
    "UserSession",
    "UserStatus",
    "canonical_login_email",
    "check_password_policy",
    "normalize_display_name",
    "normalize_email",
    "normalize_group_description",
    "normalize_group_name",
]
