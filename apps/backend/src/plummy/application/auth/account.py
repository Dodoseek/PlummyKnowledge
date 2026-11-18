"""The signed-in user's own account: sessions and password change (API_CONTRACTS.md §2.2).

Only the owner's data is visible: sessions are always filtered by the user id of the verified session, and a
session of another user is reported exactly like a missing one (404).
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from plummy.application.audit import by_user
from plummy.application.auth.sessions import IssuedSession, SessionIssuer
from plummy.application.auth.throttle import AttemptThrottle
from plummy.application.errors import AuthenticationRequiredError, InvalidInputError, NotFoundError
from plummy.application.ports import Clock, PasswordHasher, UnitOfWorkFactory
from plummy.application.security import ClientInfo, SecurityContext
from plummy.application.setup.validation import validate_password
from plummy.domain.audit import AuditAction, AuditOutcome
from plummy.domain.identity import SessionRevokeReason, SessionState, UserSession

SESSION_NOT_FOUND = "Session not found."


@dataclass(frozen=True, slots=True)
class SessionView:
    session: UserSession
    current: bool


class ListMySessions:
    def __init__(self, uow: UnitOfWorkFactory, clock: Clock, issuer: SessionIssuer) -> None:
        self._uow, self._clock, self._issuer = uow, clock, issuer

    async def execute(self, context: SecurityContext) -> list[SessionView]:
        now = self._clock.now()
        idle_ttl = self._issuer.policy.idle_ttl
        async with self._uow() as uow:
            user = await uow.users.get(context.user_id)
            if user is None:
                raise AuthenticationRequiredError()
            sessions = await uow.sessions.list_for_user(context.user_id)
        return [
            SessionView(session=session, current=session.id == context.session_id)
            for session in sessions
            if session.state(user, now=now, idle_ttl=idle_ttl) is SessionState.ACTIVE
        ]


class RevokeMySession:
    """Revoke one of the user's own sessions (the current one too: that is a logout)."""

    def __init__(self, uow: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def execute(self, context: SecurityContext, client: ClientInfo, session_id: UUID) -> None:
        now = self._clock.now()
        async with self._uow() as uow:
            session = await uow.sessions.get(session_id)
            if session is None or session.user_id != context.user_id or session.revoked_at is not None:
                raise NotFoundError(SESSION_NOT_FOUND)
            if not await uow.sessions.revoke(session.id, reason=SessionRevokeReason.LOGOUT, at=now):
                raise NotFoundError(SESSION_NOT_FOUND)
            await uow.audit.record(
                by_user(
                    context,
                    client,
                    AuditAction.AUTH_SESSION_REVOKED,
                    target_type="session",
                    target_id=session.id,
                    details={"current": session.id == context.session_id},
                )
            )
            await uow.commit()


@dataclass(frozen=True, slots=True)
class ChangePasswordCommand:
    current_password: str
    new_password: str
    client: ClientInfo


class ChangePassword:
    """Needs the current password (login throttling applies); revokes all sessions and issues a new one."""

    def __init__(
        self,
        uow: UnitOfWorkFactory,
        clock: Clock,
        hasher: PasswordHasher,
        throttle: AttemptThrottle,
        issuer: SessionIssuer,
    ) -> None:
        self._uow, self._clock, self._hasher = uow, clock, hasher
        self._throttle, self._issuer = throttle, issuer

    async def execute(self, context: SecurityContext, command: ChangePasswordCommand) -> IssuedSession:
        await self._throttle.before_login(ip_address=command.client.ip_address, email=context.email)
        async with self._uow() as uow:
            user = await uow.users.get(context.user_id)
        if user is None or not user.can_sign_in:
            raise AuthenticationRequiredError()
        if not await self._hasher.verify(user.password_hash, command.current_password):
            await self._throttle.login_failed(email=context.email)
            async with self._uow() as uow:
                await uow.audit.record(
                    by_user(
                        context,
                        command.client,
                        AuditAction.AUTH_PASSWORD_CHANGED,
                        target_type="user",
                        target_id=user.id,
                        details={"reason": "invalid_current_password"},
                        outcome=AuditOutcome.DENIED,
                    )
                )
                await uow.commit()
            raise InvalidInputError(
                "current_password", "invalid_current_password", "The current password is incorrect."
            )
        if command.new_password == command.current_password:
            raise InvalidInputError(
                "new_password", "password_unchanged", "The new password must differ from the current one."
            )
        validate_password(command.new_password, email=user.email, field="new_password")
        new_hash = await self._hasher.hash(command.new_password)
        now = self._clock.now()
        async with self._uow() as uow:
            current = await uow.users.get_for_update(user.id)
            # Changed concurrently (another password change, disable, delete) while we were hashing.
            if current is None or not current.can_sign_in or current.password_hash != user.password_hash:
                raise AuthenticationRequiredError()
            await uow.users.set_password(current.id, password_hash=new_hash, changed_at=now)
            current.password_hash, current.password_changed_at = new_hash, now
            revoked = await uow.sessions.revoke_all_for_user(
                current.id, reason=SessionRevokeReason.PASSWORD_CHANGE, at=now
            )
            issued = await self._issuer.issue(
                uow, current, now=now, client=command.client, previous_token=None
            )
            await uow.audit.record(
                by_user(
                    context,
                    command.client,
                    AuditAction.AUTH_PASSWORD_CHANGED,
                    target_type="user",
                    target_id=current.id,
                    details={"sessions_revoked": revoked},
                )
            )
            await uow.commit()
        await self._throttle.login_succeeded(email=context.email)
        return issued
