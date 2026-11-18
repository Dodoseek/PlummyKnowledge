"""Session issuing and the security context built from a session (ADR-013 §1)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from plummy.application.ports import UnitOfWork
from plummy.application.security import ClientInfo, SecurityContext
from plummy.core.ids import new_id
from plummy.core.security import generate_token, hash_token, is_well_formed_token
from plummy.domain.identity import SessionRevokeReason, User, UserSession
from plummy.domain.identity.models import MAX_USER_AGENT_LENGTH

# ``last_seen_at`` is written at most this often per session (one UPDATE instead of one per request).
TOUCH_INTERVAL = timedelta(seconds=60)


@dataclass(frozen=True, slots=True)
class SessionPolicy:
    idle_ttl: timedelta
    absolute_ttl: timedelta
    touch_interval: timedelta = TOUCH_INTERVAL


@dataclass(frozen=True, slots=True)
class IssuedSession:
    """A new session. ``token`` goes only into the cookie; it is never stored or logged."""

    token: str
    context: SecurityContext


def build_context(
    user: User,
    session: UserSession,
    roles: frozenset[str],
    permissions: frozenset[str],
    policy: SessionPolicy,
) -> SecurityContext:
    return SecurityContext(
        user_id=user.id,
        session_id=session.id,
        email=user.email,
        display_name=user.display_name,
        roles=roles,
        permissions=permissions,
        session_expires_at=session.expires_at,
        session_idle_expires_at=session.idle_expires_at(policy.idle_ttl),
    )


class SessionIssuer:
    """Creates a session inside the caller's transaction (login, setup)."""

    def __init__(self, policy: SessionPolicy) -> None:
        self._policy = policy

    @property
    def policy(self) -> SessionPolicy:
        return self._policy

    async def issue(
        self,
        uow: UnitOfWork,
        user: User,
        *,
        now: datetime,
        client: ClientInfo,
        previous_token: str | None,
    ) -> IssuedSession:
        """Always a new token (session fixation protection); the session of the previous cookie is revoked."""
        if previous_token is not None and is_well_formed_token(previous_token):
            await uow.sessions.revoke_by_token_hash(
                hash_token(previous_token), reason=SessionRevokeReason.SUPERSEDED, at=now
            )
        token = generate_token()
        session = UserSession(
            id=new_id(),
            user_id=user.id,
            created_at=now,
            last_seen_at=now,
            expires_at=now + self._policy.absolute_ttl,
            user_agent=client.user_agent[:MAX_USER_AGENT_LENGTH] if client.user_agent else None,
            ip_address=client.ip_address,
        )
        await uow.sessions.add(session, hash_token(token))
        roles, permissions = await uow.roles.roles_and_permissions(user.id)
        context = build_context(user, session, roles, permissions, self._policy)
        return IssuedSession(token=token, context=context)
