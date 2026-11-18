"""Setup use cases (ADR-006, SECURITY_MODEL.md §4.5).

Web initialization needs the one-time bootstrap token. Everything else (issuing the token, creating an
administrator, resetting a password) is available only through the CLI on the server.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from uuid import UUID

from plummy.application.auth.sessions import IssuedSession, SessionIssuer
from plummy.application.auth.throttle import AttemptThrottle
from plummy.application.errors import (
    ApplicationError,
    ConflictError,
    NotFoundError,
    SetupAlreadyCompletedError,
    SetupTokenInvalidError,
    WebSetupDisabledError,
)
from plummy.application.ports import (
    BootstrapTokenStore,
    Clock,
    PasswordHasher,
    UnitOfWork,
    UnitOfWorkFactory,
)
from plummy.application.security import ClientInfo
from plummy.application.setup.validation import (
    AdministratorInput,
    validate_administrator,
    validate_email,
    validate_password,
)
from plummy.core.ids import new_id
from plummy.core.security import generate_token, hash_token, is_well_formed_token
from plummy.domain.audit import ActorType, AuditAction, AuditEntry, AuditOutcome
from plummy.domain.identity import InstanceState, SessionRevokeReason, SystemRole, User, UserStatus

logger = logging.getLogger(__name__)

_NO_TOKEN = b"\x00" * 32


@dataclass(frozen=True, slots=True)
class SetupStatus:
    initialized: bool
    web_setup_enabled: bool


class GetSetupStatus:
    def __init__(self, uow: UnitOfWorkFactory, *, web_setup_enabled: bool) -> None:
        self._uow = uow
        self._web_setup_enabled = web_setup_enabled

    async def execute(self) -> SetupStatus:
        async with self._uow() as uow:
            state = await uow.instance.get()
        return SetupStatus(initialized=state.is_initialized, web_setup_enabled=self._web_setup_enabled)


@dataclass(frozen=True, slots=True)
class InitializeCommand:
    bootstrap_token: str
    admin: AdministratorInput
    client: ClientInfo
    previous_token: str | None = None


async def _create_administrator(
    uow: UnitOfWork, admin: AdministratorInput, password_hash: str, *, now: datetime
) -> User:
    if await uow.users.get_by_email(admin.email) is not None:
        raise ConflictError("A user with this email already exists.")
    user = User(
        id=new_id(),
        email=admin.email,
        display_name=admin.display_name,
        password_hash=password_hash,
        status=UserStatus.ACTIVE,
        created_at=now,
        password_changed_at=now,
    )
    await uow.users.add(user)
    await uow.roles.assign(user.id, SystemRole.ADMIN.value, granted_by=None)
    return user


class InitializeInstance:
    """``POST /setup/initialize``: creates the first administrator exactly once.

    Concurrency: the ``instance_state`` row is locked (``SELECT … FOR UPDATE``) and the state re-checked
    inside the transaction that creates the user, so of two parallel requests only one can succeed.
    """

    def __init__(
        self,
        uow: UnitOfWorkFactory,
        clock: Clock,
        hasher: PasswordHasher,
        throttle: AttemptThrottle,
        issuer: SessionIssuer,
        *,
        web_setup_enabled: bool,
    ) -> None:
        self._uow, self._clock, self._hasher = uow, clock, hasher
        self._throttle, self._issuer = throttle, issuer
        self._web_setup_enabled = web_setup_enabled

    async def execute(self, command: InitializeCommand) -> IssuedSession:
        if not self._web_setup_enabled:
            raise WebSetupDisabledError()
        await self._throttle.before_setup(ip_address=command.client.ip_address)
        token = command.bootstrap_token
        token_hash = hash_token(token) if is_well_formed_token(token) else _NO_TOKEN

        # Cheap pre-check without a lock: a wrong token is rejected before the expensive password hash.
        async with self._uow() as uow:
            state = await uow.instance.get()
        rejection = self._rejection(state, token_hash, now=self._clock.now())
        if rejection is not None:
            await self._audit_rejection(rejection, command.client)
            raise rejection

        admin = validate_administrator(command.admin, field_prefix="admin.")
        password_hash = await self._hasher.hash(admin.password)

        now = self._clock.now()
        async with self._uow() as uow:
            state = await uow.instance.get_for_update()
            rejection = self._rejection(state, token_hash, now=now)
            if rejection is None:
                user = await _create_administrator(uow, admin, password_hash, now=now)
                state.mark_initialized(by_user_id=user.id, now=now)
                await uow.instance.save(state)
                issued = await self._issuer.issue(
                    uow, user, now=now, client=command.client, previous_token=command.previous_token
                )
                await uow.audit.record(
                    AuditEntry(
                        action=AuditAction.SETUP_COMPLETED,
                        outcome=AuditOutcome.SUCCESS,
                        actor_type=ActorType.USER,
                        actor_user_id=user.id,
                        target_type="user",
                        target_id=user.id,
                        ip_address=command.client.ip_address,
                        user_agent=command.client.user_agent,
                        details={"via": "web"},
                    )
                )
                await uow.commit()
                logger.info("instance initialized", extra={"via": "web"})
                return issued
        await self._audit_rejection(rejection, command.client)
        raise rejection

    @staticmethod
    def _rejection(state: InstanceState, token_hash: bytes, *, now: datetime) -> ApplicationError | None:
        if state.is_initialized:
            return SetupAlreadyCompletedError()
        if not state.accepts_bootstrap_token(token_hash, now=now):
            return SetupTokenInvalidError()
        return None

    async def _audit_rejection(self, rejection: ApplicationError, client: ClientInfo) -> None:
        reason = "already_completed" if isinstance(rejection, SetupAlreadyCompletedError) else "token_invalid"
        logger.warning("setup initialization rejected", extra={"reason": reason})
        async with self._uow() as uow:
            await uow.audit.record(
                AuditEntry(
                    action=AuditAction.SETUP_INITIALIZE_REJECTED,
                    outcome=AuditOutcome.DENIED,
                    actor_type=ActorType.ANONYMOUS,
                    ip_address=client.ip_address,
                    user_agent=client.user_agent,
                    details={"reason": reason},
                )
            )
            await uow.commit()


class TokenIssueOutcome(StrEnum):
    ISSUED = "issued"
    UNCHANGED = "unchanged"
    ALREADY_INITIALIZED = "already_initialized"


@dataclass(frozen=True, slots=True)
class TokenIssueResult:
    outcome: TokenIssueOutcome
    location: str
    expires_at: datetime | None


class IssueBootstrapToken:
    """CLI ``setup init-token [--rotate]``. Idempotent: a valid token that matches the file is kept.

    The plain token is written only to the server-side store, never printed or logged. The old file is
    removed before the new hash is committed and the new file is written after the commit, so a failure at
    any point leaves "no file" and the next run issues a fresh token.
    """

    def __init__(
        self, uow: UnitOfWorkFactory, clock: Clock, store: BootstrapTokenStore, *, ttl: timedelta
    ) -> None:
        self._uow, self._clock, self._store, self._ttl = uow, clock, store, ttl

    async def execute(self, *, rotate: bool = False) -> TokenIssueResult:
        now = self._clock.now()
        async with self._uow() as uow:
            state = await uow.instance.get_for_update()
            if state.is_initialized:
                self._store.remove()
                return TokenIssueResult(TokenIssueOutcome.ALREADY_INITIALIZED, self._store.location, None)
            current = self._store.read()
            if (
                not rotate
                and current is not None
                and is_well_formed_token(current)
                and state.accepts_bootstrap_token(hash_token(current), now=now)
            ):
                return TokenIssueResult(
                    TokenIssueOutcome.UNCHANGED, self._store.location, state.bootstrap_token_expires_at
                )
            token = generate_token()
            expires_at = now + self._ttl
            self._store.remove()
            state.issue_bootstrap_token(hash_token(token), expires_at=expires_at)
            await uow.instance.save(state)
            await uow.audit.record(
                AuditEntry(
                    action=AuditAction.SETUP_TOKEN_ISSUED,
                    outcome=AuditOutcome.SUCCESS,
                    actor_type=ActorType.SYSTEM,
                    details={"rotated": rotate},
                )
            )
            await uow.commit()
        self._store.write(token)
        return TokenIssueResult(TokenIssueOutcome.ISSUED, self._store.location, expires_at)


@dataclass(frozen=True, slots=True)
class CreatedAdministrator:
    user_id: UUID
    initialized_instance: bool


class CreateAdministrator:
    """CLI ``admin create``: an administrator created by the server operator.

    On an uninitialized instance this *is* the setup (``SETUP_MODE=cli_only`` or a lost bootstrap token):
    the instance is marked initialized and any bootstrap token is spent.
    """

    def __init__(
        self, uow: UnitOfWorkFactory, clock: Clock, hasher: PasswordHasher, store: BootstrapTokenStore
    ) -> None:
        self._uow, self._clock, self._hasher, self._store = uow, clock, hasher, store

    async def execute(self, raw: AdministratorInput) -> CreatedAdministrator:
        admin = validate_administrator(raw)
        password_hash = await self._hasher.hash(admin.password)
        now = self._clock.now()
        async with self._uow() as uow:
            state = await uow.instance.get_for_update()
            user = await _create_administrator(uow, admin, password_hash, now=now)
            initialized = not state.is_initialized
            if initialized:
                state.mark_initialized(by_user_id=user.id, now=now)
                await uow.instance.save(state)
                await uow.audit.record(
                    AuditEntry(
                        action=AuditAction.SETUP_COMPLETED,
                        outcome=AuditOutcome.SUCCESS,
                        actor_type=ActorType.SYSTEM,
                        target_type="user",
                        target_id=user.id,
                        details={"via": "cli"},
                    )
                )
            await uow.audit.record(
                AuditEntry(
                    action=AuditAction.ADMIN_CREATED_VIA_CLI,
                    outcome=AuditOutcome.SUCCESS,
                    actor_type=ActorType.SYSTEM,
                    target_type="user",
                    target_id=user.id,
                )
            )
            await uow.commit()
        if initialized:
            self._store.remove()
        return CreatedAdministrator(user_id=user.id, initialized_instance=initialized)


class ResetPassword:
    """CLI ``admin reset-password``: sets a new password and revokes all sessions of the user."""

    def __init__(self, uow: UnitOfWorkFactory, clock: Clock, hasher: PasswordHasher) -> None:
        self._uow, self._clock, self._hasher = uow, clock, hasher

    async def execute(self, *, email: str, password: str) -> int:
        canonical = validate_email(email, field="email")
        validate_password(password, email=canonical, field="password")
        password_hash = await self._hasher.hash(password)
        now = self._clock.now()
        async with self._uow() as uow:
            user = await uow.users.get_by_email(canonical)
            if user is None or user.deleted_at is not None:
                raise NotFoundError("No user with this email.")
            await uow.users.set_password(user.id, password_hash=password_hash, changed_at=now)
            revoked = await uow.sessions.revoke_all_for_user(
                user.id, reason=SessionRevokeReason.PASSWORD_CHANGE, at=now
            )
            await uow.audit.record(
                AuditEntry(
                    action=AuditAction.ADMIN_PASSWORD_RESET_VIA_CLI,
                    outcome=AuditOutcome.SUCCESS,
                    actor_type=ActorType.SYSTEM,
                    target_type="user",
                    target_id=user.id,
                    details={"sessions_revoked": revoked},
                )
            )
            await uow.commit()
        return revoked
