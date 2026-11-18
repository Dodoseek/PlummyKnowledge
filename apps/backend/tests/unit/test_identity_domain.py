from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from plummy.core.security import hash_token
from plummy.domain.identity import (
    SYSTEM_ROLE_PERMISSIONS,
    InstanceState,
    InvalidDisplayNameError,
    InvalidEmailError,
    PasswordPolicyError,
    Permission,
    SessionState,
    SystemRole,
    User,
    UserSession,
    UserStatus,
    canonical_login_email,
    check_password_policy,
    normalize_display_name,
    normalize_email,
)

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)
IDLE = timedelta(hours=24)


# --- password policy ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "password", ["a" * 11 + "b", "x" * 127 + "y", "пароль-из-кириллицы", "correct horse battery"]
)
def test_acceptable_passwords(password: str) -> None:
    check_password_policy(password, email="user@example.com")


@pytest.mark.parametrize(
    ("password", "code"),
    [
        ("", "password_too_short"),
        ("a" * 10 + "b", "password_too_short"),  # 11 characters: boundary
        ("a" * 128 + "b", "password_too_long"),  # 129 characters: boundary
        ("z" * 20, "password_repetitive"),
        ("User@Example.com", "password_matches_email"),
    ],
)
def test_rejected_passwords(password: str, code: str) -> None:
    with pytest.raises(PasswordPolicyError) as error:
        check_password_policy(password, email="user@example.com")
    assert error.value.code == code
    assert password not in str(error.value) or not password


# --- email ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (" Admin@Example.COM ", "admin@example.com"),
        ("ops@intranet", "ops@intranet"),  # self-hosted: single-label domains are allowed
        ("Ivan@Пример.рф", "ivan@пример.рф"),
    ],
)
def test_email_normalization(raw: str, expected: str) -> None:
    assert normalize_email(raw) == expected


@pytest.mark.parametrize("raw", ["", "plain", "a@@b.com", "a@b..com", "x" * 250 + "@example.com"])
def test_invalid_email(raw: str) -> None:
    with pytest.raises(InvalidEmailError) as error:
        normalize_email(raw)
    assert raw not in str(error.value) or not raw


def test_login_email_never_raises() -> None:
    assert canonical_login_email(" NOT AN EMAIL ") == "not an email"
    assert canonical_login_email("Admin@Example.com") == "admin@example.com"


# --- display name ---------------------------------------------------------------------------------------


def test_display_name_rules() -> None:
    assert normalize_display_name("  Ivan  ") == "Ivan"
    assert normalize_display_name("x" * 200) == "x" * 200
    for bad in ("", "   ", "x" * 201, "bad\nname", "bad\x00name"):
        with pytest.raises(InvalidDisplayNameError):
            normalize_display_name(bad)


# --- sessions -------------------------------------------------------------------------------------------


def _user(**changes: object) -> User:
    user = User(
        id=uuid4(),
        email="u@example.com",
        display_name="U",
        password_hash="$argon2id$...",
        status=UserStatus.ACTIVE,
        created_at=NOW - timedelta(days=1),
        password_changed_at=NOW - timedelta(days=1),
    )
    for key, value in changes.items():
        setattr(user, key, value)
    return user


def _session(**changes: object) -> UserSession:
    session = UserSession(
        id=uuid4(),
        user_id=uuid4(),
        created_at=NOW - timedelta(hours=1),
        last_seen_at=NOW - timedelta(minutes=5),
        expires_at=NOW + timedelta(days=6),
    )
    for key, value in changes.items():
        setattr(session, key, value)
    return session


def test_active_session() -> None:
    assert _session().state(_user(), now=NOW, idle_ttl=IDLE) is SessionState.ACTIVE


@pytest.mark.parametrize(
    ("session_changes", "user_changes", "expected"),
    [
        ({"revoked_at": NOW}, {}, SessionState.REVOKED),
        ({"expires_at": NOW}, {}, SessionState.EXPIRED),  # boundary: expires exactly now
        ({"last_seen_at": NOW - IDLE}, {}, SessionState.EXPIRED),
        ({}, {"status": UserStatus.DISABLED}, SessionState.EXPIRED),
        ({}, {"deleted_at": NOW}, SessionState.EXPIRED),
        ({}, {"password_changed_at": NOW - timedelta(minutes=1)}, SessionState.EXPIRED),
    ],
)
def test_session_rejections(
    session_changes: dict[str, object], user_changes: dict[str, object], expected: SessionState
) -> None:
    assert _session(**session_changes).state(_user(**user_changes), now=NOW, idle_ttl=IDLE) is expected


def test_idle_expiry_never_exceeds_absolute_expiry() -> None:
    session = _session(expires_at=NOW + timedelta(hours=1), last_seen_at=NOW)
    assert session.idle_expires_at(IDLE) == NOW + timedelta(hours=1)


# --- instance state -------------------------------------------------------------------------------------


def _state() -> InstanceState:
    return InstanceState(None, None, None, None, None)


def test_bootstrap_token_lifecycle() -> None:
    state = _state()
    token_hash = hash_token("t" * 43)
    assert not state.accepts_bootstrap_token(token_hash, now=NOW)  # no token issued yet
    state.issue_bootstrap_token(token_hash, expires_at=NOW + timedelta(hours=1))
    assert state.accepts_bootstrap_token(token_hash, now=NOW)
    assert not state.accepts_bootstrap_token(hash_token("u" * 43), now=NOW)
    assert not state.accepts_bootstrap_token(token_hash, now=NOW + timedelta(hours=1))  # expired

    user_id = uuid4()
    state.mark_initialized(by_user_id=user_id, now=NOW)
    assert state.is_initialized
    assert state.initialized_by_user_id == user_id
    assert state.bootstrap_token_hash is None
    assert state.bootstrap_token_used_at == NOW
    assert not state.accepts_bootstrap_token(token_hash, now=NOW)
    with pytest.raises(ValueError, match="already initialized"):
        state.mark_initialized(by_user_id=uuid4(), now=NOW)
    with pytest.raises(ValueError, match="already initialized"):
        state.issue_bootstrap_token(token_hash, expires_at=NOW + timedelta(hours=1))


# --- permissions ----------------------------------------------------------------------------------------


def test_role_matrix() -> None:
    assert SYSTEM_ROLE_PERMISSIONS[SystemRole.ADMIN] == frozenset(Permission)
    assert SYSTEM_ROLE_PERMISSIONS[SystemRole.MEMBER] == {Permission.CHAT_USE}
    assert SYSTEM_ROLE_PERMISSIONS[SystemRole.AUDITOR] == {Permission.AUDIT_READ}
    manager = SYSTEM_ROLE_PERMISSIONS[SystemRole.KNOWLEDGE_MANAGER]
    assert Permission.KNOWLEDGE_BASES_MANAGE in manager
    assert Permission.USERS_MANAGE not in manager
    assert Permission.SETTINGS_MANAGE not in manager
    # ADR-012: no platform/organization permissions.
    assert not [p for p in Permission if p.value.startswith(("platform.", "org."))]
