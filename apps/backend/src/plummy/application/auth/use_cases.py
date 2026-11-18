"""Authentication use cases: authenticate a session, login, logout (ADR-013)."""

from __future__ import annotations

from dataclasses import dataclass

from plummy.application.auth.sessions import IssuedSession, SessionIssuer, SessionPolicy, build_context
from plummy.application.auth.throttle import AttemptThrottle
from plummy.application.errors import (
    AuthenticationRequiredError,
    InvalidCredentialsError,
    SessionRevokedError,
)
from plummy.application.ports import Clock, PasswordHasher, UnitOfWorkFactory
from plummy.application.security import ClientInfo, SecurityContext
from plummy.core.security import hash_token, is_well_formed_token
from plummy.domain.audit import ActorType, AuditAction, AuditEntry, AuditOutcome
from plummy.domain.identity import SessionRevokeReason, SessionState, canonical_login_email

INVALID_CREDENTIALS_MESSAGE = "Invalid email or password."


class AuthenticateSession:
    """Validates the session token on every request (one indexed lookup + roles/permissions)."""

    def __init__(self, uow: UnitOfWorkFactory, clock: Clock, policy: SessionPolicy) -> None:
        self._uow, self._clock, self._policy = uow, clock, policy

    async def execute(self, token: str) -> SecurityContext:
        if not is_well_formed_token(token):
            raise AuthenticationRequiredError()
        now = self._clock.now()
        async with self._uow() as uow:
            found = await uow.sessions.get_by_token_hash(hash_token(token))
            if found is None:
                raise AuthenticationRequiredError()
            session, user = found.session, found.user
            state = session.state(user, now=now, idle_ttl=self._policy.idle_ttl)
            if state is SessionState.REVOKED:
                raise SessionRevokedError()
            if state is not SessionState.ACTIVE:
                raise AuthenticationRequiredError()
            roles, permissions = await uow.roles.roles_and_permissions(user.id)
            if now - session.last_seen_at >= self._policy.touch_interval:
                await uow.sessions.touch(session.id, at=now)
                session.last_seen_at = now
                await uow.commit()
        return build_context(user, session, roles, permissions, self._policy)


@dataclass(frozen=True, slots=True)
class LoginCommand:
    email: str
    password: str
    client: ClientInfo
    previous_token: str | None = None


class Login:
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

    async def execute(self, command: LoginCommand) -> IssuedSession:
        email = canonical_login_email(command.email)
        await self._throttle.before_login(ip_address=command.client.ip_address, email=email)
        async with self._uow() as uow:
            user = await uow.users.get_by_email(email)
        # Hashing runs outside any transaction; an unknown user is verified against a dummy hash.
        password_ok = await self._hasher.verify(user.password_hash if user else None, command.password)
        if user is None or not password_ok or not user.can_sign_in:
            reason = "unknown_user"
            if user is not None:
                reason = "invalid_password" if not password_ok else "inactive"
            await self._throttle.login_failed(email=email)
            async with self._uow() as uow:
                await uow.audit.record(
                    AuditEntry(
                        action=AuditAction.AUTH_LOGIN_FAILED,
                        outcome=AuditOutcome.DENIED,
                        actor_type=ActorType.ANONYMOUS,
                        target_type="user" if user else None,
                        target_id=user.id if user else None,
                        ip_address=command.client.ip_address,
                        user_agent=command.client.user_agent,
                        details={"reason": reason},
                    )
                )
                await uow.commit()
            raise InvalidCredentialsError(INVALID_CREDENTIALS_MESSAGE)
        new_hash: str | None = None
        if user.password_hash is not None and self._hasher.needs_rehash(user.password_hash):
            new_hash = await self._hasher.hash(command.password)
        now = self._clock.now()
        async with self._uow() as uow:
            current = await uow.users.get(user.id)
            # The account may have been disabled or its password changed while we were hashing.
            if current is None or not current.can_sign_in or current.password_hash != user.password_hash:
                raise InvalidCredentialsError(INVALID_CREDENTIALS_MESSAGE)
            issued = await self._issuer.issue(
                uow, current, now=now, client=command.client, previous_token=command.previous_token
            )
            await uow.users.record_login(current.id, at=now, new_password_hash=new_hash)
            await uow.audit.record(
                AuditEntry(
                    action=AuditAction.AUTH_LOGIN_SUCCEEDED,
                    outcome=AuditOutcome.SUCCESS,
                    actor_type=ActorType.USER,
                    actor_user_id=current.id,
                    target_type="session",
                    target_id=issued.context.session_id,
                    ip_address=command.client.ip_address,
                    user_agent=command.client.user_agent,
                )
            )
            await uow.commit()
        await self._throttle.login_succeeded(email=email)
        return issued


class Logout:
    """Revokes the current session. The API calls it only for a valid, CSRF-checked session."""

    def __init__(self, uow: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def execute(self, context: SecurityContext, client: ClientInfo) -> None:
        now = self._clock.now()
        async with self._uow() as uow:
            revoked = await uow.sessions.revoke(context.session_id, reason=SessionRevokeReason.LOGOUT, at=now)
            if revoked:
                await uow.audit.record(
                    AuditEntry(
                        action=AuditAction.AUTH_LOGOUT,
                        outcome=AuditOutcome.SUCCESS,
                        actor_type=ActorType.USER,
                        actor_user_id=context.user_id,
                        target_type="session",
                        target_id=context.session_id,
                        ip_address=client.ip_address,
                        user_agent=client.user_agent,
                    )
                )
            await uow.commit()
