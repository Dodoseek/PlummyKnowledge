"""User management by administrators (API_CONTRACTS.md §2.4, ADR-015).

Invariants enforced here, not in routers:
- every operation checks permissions from the verified session;
- the instance always keeps at least one active administrator: changes that could remove one take a row lock
  on the ``admin`` role first, so concurrent requests are serialized;
- administrators cannot disable or delete their own account.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

from plummy.application.audit import by_user, joined
from plummy.application.errors import (
    EmailTakenError,
    InvalidInputError,
    InvalidUserStateError,
    LastAdministratorError,
    NotFoundError,
    SelfModificationError,
)
from plummy.application.pagination import Page, PageRequest
from plummy.application.ports import Clock, UniqueViolationError, UnitOfWork, UnitOfWorkFactory, UserQuery
from plummy.application.security import ClientInfo, SecurityContext
from plummy.application.setup.validation import validate_email
from plummy.core.ids import new_id
from plummy.core.security import generate_token, hash_token
from plummy.domain.audit import AuditAction
from plummy.domain.identity import (
    Group,
    InvalidDisplayNameError,
    Permission,
    SessionRevokeReason,
    SystemRole,
    User,
    UserStatus,
    normalize_display_name,
)

ACTIVATION_TOKEN_TTL = timedelta(hours=72)
ACTIVATION_PATH = "/activate"
ADMIN = SystemRole.ADMIN.value
USER_NOT_FOUND = "User not found."


@dataclass(frozen=True, slots=True)
class UserView:
    user: User
    roles: frozenset[str]


@dataclass(frozen=True, slots=True)
class UserDetails:
    user: User
    roles: frozenset[str]
    groups: list[Group] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ActivationLink:
    """Shown once to the administrator; the token itself is never stored or logged."""

    url: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class CreatedUser:
    details: UserDetails
    activation: ActivationLink


class ActivationLinks:
    """Issues tokens and builds ``{PUBLIC_URL}/activate#token=…`` (a fragment is never sent to servers)."""

    def __init__(self, public_url: str | None, ttl: timedelta = ACTIVATION_TOKEN_TTL) -> None:
        self._base = (public_url or "").rstrip("/")
        self._ttl = ttl

    async def issue(self, uow: UnitOfWork, user_id: UUID, *, now: datetime, by: UUID) -> ActivationLink:
        token = generate_token()
        expires_at = now + self._ttl
        await uow.activations.put(user_id, hash_token(token), expires_at=expires_at, created_by=by)
        return ActivationLink(url=f"{self._base}{ACTIVATION_PATH}#token={token}", expires_at=expires_at)


def validate_display_name(value: str, *, field: str = "display_name") -> str:
    try:
        return normalize_display_name(value)
    except InvalidDisplayNameError as exc:
        raise InvalidInputError(field, "invalid_display_name", str(exc)) from None


async def validate_role_keys(uow: UnitOfWork, keys: frozenset[str], *, field: str = "roles") -> None:
    unknown = keys - await uow.roles.existing_keys()
    if unknown:
        raise InvalidInputError(field, "unknown_role", f"Unknown roles: {', '.join(sorted(unknown))}.")


async def _load_user(uow: UnitOfWork, user_id: UUID, *, for_update: bool = False) -> User:
    user = await (uow.users.get_for_update(user_id) if for_update else uow.users.get(user_id))
    if user is None or user.deleted_at is not None:
        raise NotFoundError(USER_NOT_FOUND)
    return user


async def _details(uow: UnitOfWork, user: User) -> UserDetails:
    return UserDetails(
        user=user,
        roles=await uow.roles.user_role_keys(user.id),
        groups=await uow.groups.groups_of_user(user.id),
    )


async def _ensure_another_administrator(uow: UnitOfWork, user: User) -> None:
    """Raise if ``user`` is the last active administrator. Call after ``lock_administrators``."""
    if user.status is not UserStatus.ACTIVE or user.deleted_at is not None:
        return
    if ADMIN not in await uow.roles.user_role_keys(user.id):
        return
    if await uow.roles.count_active_administrators(excluding=user.id) == 0:
        raise LastAdministratorError("The instance must keep at least one active administrator.")


class ListUsers:
    def __init__(self, uow: UnitOfWorkFactory) -> None:
        self._uow = uow

    async def execute(self, context: SecurityContext, query: UserQuery, page: PageRequest) -> Page[UserView]:
        context.require(Permission.USERS_READ)
        async with self._uow() as uow:
            result = await uow.users.page(query, page)
            roles = await uow.roles.role_keys_for_users([user.id for user in result.items])
        items = [UserView(user=user, roles=roles.get(user.id, frozenset())) for user in result.items]
        return Page(items=items, next_cursor=result.next_cursor)


class GetUser:
    def __init__(self, uow: UnitOfWorkFactory) -> None:
        self._uow = uow

    async def execute(self, context: SecurityContext, user_id: UUID) -> UserDetails:
        context.require(Permission.USERS_READ)
        async with self._uow() as uow:
            return await _details(uow, await _load_user(uow, user_id))


@dataclass(frozen=True, slots=True)
class CreateUserCommand:
    email: str
    display_name: str
    roles: frozenset[str] = frozenset()


class CreateUser:
    """Creates an ``invited`` user without a password and returns a one-time activation link (ADR-015)."""

    def __init__(self, uow: UnitOfWorkFactory, clock: Clock, links: ActivationLinks) -> None:
        self._uow, self._clock, self._links = uow, clock, links

    async def execute(
        self, context: SecurityContext, client: ClientInfo, command: CreateUserCommand
    ) -> CreatedUser:
        context.require(Permission.USERS_MANAGE)
        if command.roles:
            context.require(Permission.ROLES_ASSIGN)
        email = validate_email(command.email, field="email")
        display_name = validate_display_name(command.display_name)
        now = self._clock.now()
        async with self._uow() as uow:
            await validate_role_keys(uow, command.roles)
            if await uow.users.get_by_email(email) is not None:
                raise EmailTakenError("A user with this email already exists.")
            user = User(
                id=new_id(),
                email=email,
                display_name=display_name,
                password_hash=None,
                status=UserStatus.INVITED,
                created_at=now,
            )
            try:
                await uow.users.add(user)
            except UniqueViolationError:
                raise EmailTakenError("A user with this email already exists.") from None
            await uow.roles.set_user_roles(user.id, command.roles, granted_by=context.user_id)
            link = await self._links.issue(uow, user.id, now=now, by=context.user_id)
            await uow.audit.record(
                by_user(
                    context,
                    client,
                    AuditAction.USER_CREATED,
                    target_type="user",
                    target_id=user.id,
                    details={"roles": joined(command.roles), "status": user.status.value},
                )
            )
            await uow.commit()
        return CreatedUser(details=UserDetails(user=user, roles=command.roles), activation=link)


@dataclass(frozen=True, slots=True)
class UpdateUserCommand:
    display_name: str | None = None
    status: UserStatus | None = None


class UpdateUser:
    """Rename, disable or re-enable. Disabling revokes every session of the user immediately."""

    def __init__(self, uow: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def execute(
        self, context: SecurityContext, client: ClientInfo, user_id: UUID, command: UpdateUserCommand
    ) -> UserDetails:
        context.require(Permission.USERS_MANAGE)
        if command.status is UserStatus.INVITED:
            raise InvalidInputError("status", "invalid_status", "status must be 'active' or 'disabled'.")
        display_name = (
            validate_display_name(command.display_name) if command.display_name is not None else None
        )
        if command.status is UserStatus.DISABLED and user_id == context.user_id:
            raise SelfModificationError("You cannot disable your own account.")
        now = self._clock.now()
        async with self._uow() as uow:
            if command.status is UserStatus.DISABLED:
                await uow.roles.lock_administrators()
            user = await _load_user(uow, user_id, for_update=True)
            changed: list[str] = []
            if display_name is not None and display_name != user.display_name:
                user.display_name = display_name
                changed.append("display_name")
            status_action: AuditAction | None = None
            if command.status is UserStatus.DISABLED and user.status is not UserStatus.DISABLED:
                await _ensure_another_administrator(uow, user)
                user.status = UserStatus.DISABLED
                status_action = AuditAction.USER_DISABLED
            elif command.status is UserStatus.ACTIVE and user.status is not UserStatus.ACTIVE:
                if user.status is UserStatus.INVITED:
                    raise InvalidUserStateError(
                        "An invited user becomes active by using the activation link."
                    )
                # A never-activated account goes back to "invited", not to an active account without password.
                user.status = UserStatus.ACTIVE if user.password_hash is not None else UserStatus.INVITED
                changed.append("status")
            if not changed and status_action is None:
                return await _details(uow, user)
            await uow.users.update(user, at=now)
            details: dict[str, str | int | bool] = {}
            if status_action is AuditAction.USER_DISABLED:
                details["sessions_revoked"] = await uow.sessions.revoke_all_for_user(
                    user.id, reason=SessionRevokeReason.USER_DISABLED, at=now
                )
                await uow.audit.record(
                    by_user(
                        context,
                        client,
                        AuditAction.USER_DISABLED,
                        target_type="user",
                        target_id=user.id,
                        details=details,
                    )
                )
            if changed:
                await uow.audit.record(
                    by_user(
                        context,
                        client,
                        AuditAction.USER_UPDATED,
                        target_type="user",
                        target_id=user.id,
                        details={"fields": ",".join(changed), "status": user.status.value},
                    )
                )
            result = await _details(uow, user)
            await uow.commit()
        return result


class DeleteUser:
    """Soft delete: sign-in impossible, sessions revoked, memberships and roles removed, email freed."""

    def __init__(self, uow: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def execute(self, context: SecurityContext, client: ClientInfo, user_id: UUID) -> None:
        context.require(Permission.USERS_MANAGE)
        if user_id == context.user_id:
            raise SelfModificationError("You cannot delete your own account.")
        now = self._clock.now()
        async with self._uow() as uow:
            await uow.roles.lock_administrators()
            user = await _load_user(uow, user_id, for_update=True)
            await _ensure_another_administrator(uow, user)
            user.deleted_at = now
            await uow.users.update(user, at=now)
            revoked = await uow.sessions.revoke_all_for_user(
                user.id, reason=SessionRevokeReason.ADMIN, at=now
            )
            await uow.activations.delete(user.id)
            await uow.groups.remove_user_from_all(user.id)
            removed = await uow.roles.set_user_roles(user.id, frozenset(), granted_by=context.user_id)
            await uow.audit.record(
                by_user(
                    context,
                    client,
                    AuditAction.USER_DELETED,
                    target_type="user",
                    target_id=user.id,
                    details={"sessions_revoked": revoked, "roles_removed": joined(removed.removed)},
                )
            )
            await uow.commit()


class SetUserRoles:
    """Replace the user's direct roles. Removing ``admin`` from the last active administrator is refused."""

    def __init__(self, uow: UnitOfWorkFactory) -> None:
        self._uow = uow

    async def execute(
        self, context: SecurityContext, client: ClientInfo, user_id: UUID, roles: frozenset[str]
    ) -> UserDetails:
        context.require(Permission.ROLES_ASSIGN)
        async with self._uow() as uow:
            await validate_role_keys(uow, roles)
            await uow.roles.lock_administrators()
            user = await _load_user(uow, user_id, for_update=True)
            if ADMIN not in roles:
                await _ensure_another_administrator(uow, user)
            change = await uow.roles.set_user_roles(user.id, roles, granted_by=context.user_id)
            for role in sorted(change.added):
                await uow.audit.record(
                    by_user(
                        context,
                        client,
                        AuditAction.ROLE_ASSIGNED,
                        target_type="user",
                        target_id=user.id,
                        details={"role": role},
                    )
                )
            for role in sorted(change.removed):
                await uow.audit.record(
                    by_user(
                        context,
                        client,
                        AuditAction.ROLE_REVOKED,
                        target_type="user",
                        target_id=user.id,
                        details={"role": role},
                    )
                )
            result = await _details(uow, user)
            await uow.commit()
        return result


class RevokeUserSessions:
    def __init__(self, uow: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def execute(self, context: SecurityContext, client: ClientInfo, user_id: UUID) -> int:
        context.require(Permission.USERS_MANAGE)
        now = self._clock.now()
        async with self._uow() as uow:
            user = await _load_user(uow, user_id)
            revoked = await uow.sessions.revoke_all_for_user(
                user.id, reason=SessionRevokeReason.ADMIN, at=now
            )
            await uow.audit.record(
                by_user(
                    context,
                    client,
                    AuditAction.AUTH_SESSION_REVOKED_BY_ADMIN,
                    target_type="user",
                    target_id=user.id,
                    details={"sessions_revoked": revoked},
                )
            )
            await uow.commit()
        return revoked


class ReissueActivation:
    """A new activation link for an ``invited`` user; the previous one stops working."""

    def __init__(self, uow: UnitOfWorkFactory, clock: Clock, links: ActivationLinks) -> None:
        self._uow, self._clock, self._links = uow, clock, links

    async def execute(self, context: SecurityContext, client: ClientInfo, user_id: UUID) -> ActivationLink:
        context.require(Permission.USERS_MANAGE)
        now = self._clock.now()
        async with self._uow() as uow:
            user = await _load_user(uow, user_id, for_update=True)
            if user.status is not UserStatus.INVITED:
                raise InvalidUserStateError("Only invited users have an activation link.")
            link = await self._links.issue(uow, user.id, now=now, by=context.user_id)
            await uow.audit.record(
                by_user(
                    context,
                    client,
                    AuditAction.USER_ACTIVATION_REISSUED,
                    target_type="user",
                    target_id=user.id,
                )
            )
            await uow.commit()
        return link
