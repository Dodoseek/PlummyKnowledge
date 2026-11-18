"""Account activation by a one-time link (ADR-015).

The token is single-use: it is deleted in the same transaction that sets the password, under a row lock on the
user, so two concurrent activations cannot both succeed. Every failure looks the same to the client
(``activation_token_invalid``); the reason is only in the audit log.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from plummy.application.auth.sessions import IssuedSession, SessionIssuer
from plummy.application.auth.throttle import AttemptThrottle
from plummy.application.errors import ActivationTokenInvalidError
from plummy.application.ports import Clock, PasswordHasher, UnitOfWork, UnitOfWorkFactory
from plummy.application.security import ClientInfo
from plummy.application.setup.validation import validate_password
from plummy.core.security import hash_token, is_well_formed_token
from plummy.domain.audit import ActorType, AuditAction, AuditEntry, AuditOutcome
from plummy.domain.identity import User, UserStatus

INVALID_LINK = "The activation link is invalid or has expired."


@dataclass(frozen=True, slots=True)
class ActivateCommand:
    token: str
    password: str
    client: ClientInfo
    previous_token: str | None = None


class _RejectedError(Exception):
    def __init__(self, reason: str, user_id: UUID | None = None) -> None:
        super().__init__(reason)
        self.reason, self.user_id = reason, user_id


async def _usable_user(uow: UnitOfWork, token_hash: bytes, *, now: datetime, lock: bool) -> User:
    record = await uow.activations.find(token_hash)
    if record is None:
        raise _RejectedError("unknown_token")
    if now >= record.expires_at:
        raise _RejectedError("expired", record.user_id)
    user = await (uow.users.get_for_update(record.user_id) if lock else uow.users.get(record.user_id))
    if user is None or user.deleted_at is not None or user.status is not UserStatus.INVITED:
        raise _RejectedError("user_not_invited", record.user_id)
    return user


class ActivateAccount:
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

    async def execute(self, command: ActivateCommand) -> IssuedSession:
        await self._throttle.before_activation(ip_address=command.client.ip_address)
        try:
            return await self._activate(command)
        except _RejectedError as rejected:
            await self._record_rejection(command.client, rejected)
            raise ActivationTokenInvalidError(INVALID_LINK) from None

    async def _activate(self, command: ActivateCommand) -> IssuedSession:
        if not is_well_formed_token(command.token):
            raise _RejectedError("malformed_token")
        token_hash = hash_token(command.token)
        async with self._uow() as uow:
            user = await _usable_user(uow, token_hash, now=self._clock.now(), lock=False)
        # A policy violation keeps the link usable: the user just picks another password.
        validate_password(command.password, email=user.email, field="password")
        password_hash = await self._hasher.hash(command.password)
        now = self._clock.now()
        async with self._uow() as uow:
            # Checked again under the lock: the link may have been used or reissued while we were hashing.
            user = await _usable_user(uow, token_hash, now=now, lock=True)
            await uow.users.set_password(user.id, password_hash=password_hash, changed_at=now)
            user.password_hash, user.password_changed_at = password_hash, now
            user.status = UserStatus.ACTIVE
            await uow.users.update(user, at=now)
            await uow.activations.delete(user.id)
            issued = await self._issuer.issue(
                uow, user, now=now, client=command.client, previous_token=command.previous_token
            )
            await uow.audit.record(
                AuditEntry(
                    action=AuditAction.USER_ACTIVATED,
                    outcome=AuditOutcome.SUCCESS,
                    actor_type=ActorType.USER,
                    actor_user_id=user.id,
                    target_type="user",
                    target_id=user.id,
                    ip_address=command.client.ip_address,
                    user_agent=command.client.user_agent,
                )
            )
            await uow.commit()
        return issued

    async def _record_rejection(self, client: ClientInfo, rejected: _RejectedError) -> None:
        async with self._uow() as uow:
            await uow.audit.record(
                AuditEntry(
                    action=AuditAction.USER_ACTIVATION_REJECTED,
                    outcome=AuditOutcome.DENIED,
                    actor_type=ActorType.ANONYMOUS,
                    target_type="user" if rejected.user_id else None,
                    target_id=rejected.user_id,
                    ip_address=client.ip_address,
                    user_agent=client.user_agent,
                    details={"reason": rejected.reason},
                )
            )
            await uow.commit()
