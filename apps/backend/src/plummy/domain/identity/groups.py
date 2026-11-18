"""Groups: named sets of users that receive roles (and, later, knowledge-base access)."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from plummy.domain.identity.permissions import SystemRole

MAX_GROUP_NAME_LENGTH = 100
MAX_GROUP_DESCRIPTION_LENGTH = 500

# The administrator role is granted to users directly, never through a group: the "at least one active
# administrator" invariant then depends only on direct assignments, and every admin grant is explicit.
ROLES_NOT_ASSIGNABLE_TO_GROUPS = frozenset({SystemRole.ADMIN.value})

_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Zl", "Zp"})


class InvalidGroupNameError(ValueError):
    pass


class InvalidGroupDescriptionError(ValueError):
    pass


def normalize_group_name(value: str) -> str:
    name = value.strip()
    if not name or len(name) > MAX_GROUP_NAME_LENGTH:
        raise InvalidGroupNameError(f"Group name must be 1-{MAX_GROUP_NAME_LENGTH} characters long.")
    if any(unicodedata.category(char) in _FORBIDDEN_CATEGORIES for char in name):
        raise InvalidGroupNameError("Group name must not contain control characters.")
    return name


def normalize_group_description(value: str | None) -> str | None:
    """Trimmed description; empty means none. Line breaks are allowed, other control characters are not."""
    if value is None:
        return None
    description = value.strip()
    if not description:
        return None
    if len(description) > MAX_GROUP_DESCRIPTION_LENGTH:
        raise InvalidGroupDescriptionError(
            f"Description must be at most {MAX_GROUP_DESCRIPTION_LENGTH} characters long."
        )
    if any(
        unicodedata.category(char) in _FORBIDDEN_CATEGORIES and char not in "\n\t" for char in description
    ):
        raise InvalidGroupDescriptionError("Description must not contain control characters.")
    return description


@dataclass(slots=True)
class Group:
    id: UUID
    name: str
    description: str | None
    created_at: datetime
    deleted_at: datetime | None = None
