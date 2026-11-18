"""AI providers and their chat models (API_CONTRACTS.md §2.7).

- API keys are write-only: stored encrypted (ADR-016), never returned, logged or audited; only a 4-character
  hint of long keys is shown.
- ``base_url`` is checked against the instance network policy on create/update (``422 endpoint_not_allowed``)
  and again on every connection by ``SafeHttpClient`` (ADR-007).
- A connection test returns only a typed error code, never the provider's response.
- Network calls never run inside a database transaction.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Final
from uuid import UUID

from plummy.application.ai.ports import (
    EndpointNotAllowed,
    InvalidEndpoint,
    ProviderConnection,
    ProviderEgressDeniedError,
    ProviderError,
    ProviderGateway,
)
from plummy.application.audit import by_user
from plummy.application.auth.throttle import Limit
from plummy.application.errors import (
    EndpointNotAllowedError,
    InvalidInputError,
    ModelIdTakenError,
    NotFoundError,
    ProviderInUseError,
    ProviderNameTakenError,
    RateLimitedError,
    ServiceUnavailableError,
)
from plummy.application.pagination import Page, PageRequest
from plummy.application.ports import (
    Clock,
    RateLimiter,
    RateLimiterUnavailableError,
    UniqueViolationError,
    UnitOfWork,
    UnitOfWorkFactory,
)
from plummy.application.secret_storage.vault import SecretVault, validate_secret
from plummy.application.security import ClientInfo, SecurityContext
from plummy.core.ids import new_id
from plummy.core.network.policy import EgressPurpose
from plummy.domain.ai import (
    DEFAULT_TIMEOUT_SECONDS,
    KEY_REQUIRED,
    AIProvider,
    InvalidAIConfigError,
    ModelConfig,
    ProviderType,
    TestStatus,
    normalize_display_name,
    normalize_name,
    validate_model_id,
    validate_timeout,
    validate_token_count,
)
from plummy.domain.audit import AuditAction, AuditOutcome
from plummy.domain.identity import Permission
from plummy.domain.secrets import SecretKind, SecretValue

PROVIDER_NOT_FOUND: Final = "Provider not found."
MODEL_NOT_FOUND: Final = "Model not found."
NAME_TAKEN: Final = "A provider with this name already exists."
MAX_BASE_URL_LENGTH: Final = 2048
MAX_TEST_SECONDS: Final = 30
MAX_REPORTED_MODELS: Final = 500
PROVIDER_TEST_PER_MINUTE: Final = Limit("provider_test:instance:1m", 10, 60)


def invalid(exc: InvalidAIConfigError) -> InvalidInputError:
    return InvalidInputError(exc.field, exc.code, str(exc))


def _base_url(value: str) -> str:
    url = value.strip().rstrip("/")
    if not url or len(url) > MAX_BASE_URL_LENGTH:
        raise InvalidInputError("base_url", "invalid_url", "The base URL is not a valid http(s) endpoint.")
    return url


async def check_endpoint(gateway: ProviderGateway, base_url: str, provider_type: ProviderType) -> None:
    try:
        await gateway.check_endpoint(base_url, provider_type)
    except InvalidEndpoint:
        raise InvalidInputError(
            "base_url", "invalid_url", "The base URL is not a valid http(s) endpoint."
        ) from None
    except EndpointNotAllowed as exc:
        raise EndpointNotAllowedError(exc.reason) from None


async def load_provider(uow: UnitOfWork, provider_id: UUID, *, for_update: bool = False) -> AIProvider:
    provider = await (
        uow.providers.get_for_update(provider_id) if for_update else uow.providers.get(provider_id)
    )
    if provider is None:
        raise NotFoundError(PROVIDER_NOT_FOUND)
    return provider


async def enforce_test_limit(limiter: RateLimiter) -> None:
    """``provider_test``: 10 per minute for the whole installation (API_CONTRACTS.md §5)."""
    limit = PROVIDER_TEST_PER_MINUTE
    try:
        status = await limiter.hit(limit.name, limit=limit.limit, window_seconds=limit.window_seconds)
    except RateLimiterUnavailableError:
        raise ServiceUnavailableError() from None
    if not status.allowed:
        raise RateLimitedError(status.retry_after_seconds)


async def connection_for(uow: UnitOfWork, vault: SecretVault, provider: AIProvider) -> ProviderConnection:
    """Decrypts the key at the point of use. Raises ``secret_unavailable`` if the key ring lost its key."""
    key = await vault.reveal(uow, provider.secret_id) if provider.secret_id is not None else None
    return ProviderConnection(
        provider.provider_type, provider.base_url, key, timeout_seconds=float(provider.timeout_seconds)
    )


def _secret(provider_type: ProviderType, api_key: str | None) -> SecretValue | None:
    if api_key is None:
        if provider_type in KEY_REQUIRED:
            raise InvalidInputError(
                "api_key", "secret_required", "An API key is required for this provider type."
            )
        return None
    return validate_secret("api_key", api_key)


@dataclass(frozen=True, slots=True)
class ProviderInput:
    name: str
    provider_type: ProviderType
    base_url: str | None
    api_key: str | None = field(default=None, repr=False)
    timeout_seconds: int | None = None
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class ProviderPatch:
    """``api_key_set`` distinguishes "not sent" (unchanged) from ``null`` (remove) and a string (replace)."""

    name: str | None = None
    base_url: str | None = None
    timeout_seconds: int | None = None
    enabled: bool | None = None
    api_key: str | None = field(default=None, repr=False)
    api_key_set: bool = False


@dataclass(frozen=True, slots=True)
class ConnectionTestReport:
    status: TestStatus
    latency_ms: int
    tested_at: datetime
    models: tuple[str, ...] = ()
    error: ProviderError | None = None


class ListProviders:
    def __init__(self, uow: UnitOfWorkFactory) -> None:
        self._uow = uow

    async def execute(self, context: SecurityContext, page: PageRequest) -> Page[AIProvider]:
        context.require(Permission.PROVIDERS_READ)
        async with self._uow() as uow:
            return await uow.providers.page(page)


class GetProvider:
    def __init__(self, uow: UnitOfWorkFactory) -> None:
        self._uow = uow

    async def execute(self, context: SecurityContext, provider_id: UUID) -> AIProvider:
        context.require(Permission.PROVIDERS_READ)
        async with self._uow() as uow:
            return await load_provider(uow, provider_id)


class CreateProvider:
    def __init__(
        self, uow: UnitOfWorkFactory, clock: Clock, vault: SecretVault, gateway: ProviderGateway
    ) -> None:
        self._uow, self._clock, self._vault, self._gateway = uow, clock, vault, gateway

    async def execute(self, context: SecurityContext, client: ClientInfo, data: ProviderInput) -> AIProvider:
        context.require(Permission.PROVIDERS_MANAGE)
        try:
            name = normalize_name("name", data.name)
            timeout = validate_timeout(data.timeout_seconds or DEFAULT_TIMEOUT_SECONDS)
        except InvalidAIConfigError as exc:
            raise invalid(exc) from None
        raw_url = (
            data.base_url if data.base_url is not None else self._gateway.default_base_url(data.provider_type)
        )
        if raw_url is None:
            raise InvalidInputError(
                "base_url", "base_url_required", "A base URL is required for this provider type."
            )
        base_url = _base_url(raw_url)
        secret = _secret(data.provider_type, data.api_key)
        await check_endpoint(self._gateway, base_url, data.provider_type)
        now = self._clock.now()
        async with self._uow() as uow:
            if await uow.providers.get_by_name(name) is not None:
                raise ProviderNameTakenError(NAME_TAKEN)
            record = await self._vault.store(uow, SecretKind.AI_API_KEY, secret) if secret else None
            provider = AIProvider(
                id=new_id(),
                name=name,
                provider_type=data.provider_type,
                base_url=base_url,
                secret_id=record.id if record else None,
                secret_hint=record.hint if record else None,
                timeout_seconds=timeout,
                enabled=data.enabled,
                created_at=now,
                updated_at=now,
            )
            try:
                await uow.providers.add(provider)
            except UniqueViolationError:
                raise ProviderNameTakenError(NAME_TAKEN) from None
            await uow.audit.record(
                by_user(
                    context,
                    client,
                    AuditAction.PROVIDER_CREATED,
                    target_type="ai_provider",
                    target_id=provider.id,
                    details={"provider_type": str(provider.provider_type), "has_secret": record is not None},
                )
            )
            await uow.commit()
        return provider


class UpdateProvider:
    def __init__(
        self, uow: UnitOfWorkFactory, clock: Clock, vault: SecretVault, gateway: ProviderGateway
    ) -> None:
        self._uow, self._clock, self._vault, self._gateway = uow, clock, vault, gateway

    async def execute(
        self, context: SecurityContext, client: ClientInfo, provider_id: UUID, patch: ProviderPatch
    ) -> AIProvider:
        context.require(Permission.PROVIDERS_MANAGE)
        try:
            name = normalize_name("name", patch.name) if patch.name is not None else None
            timeout = validate_timeout(patch.timeout_seconds) if patch.timeout_seconds is not None else None
        except InvalidAIConfigError as exc:
            raise invalid(exc) from None
        base_url = _base_url(patch.base_url) if patch.base_url is not None else None
        async with (
            self._uow() as uow
        ):  # the type is immutable: read it to validate the endpoint outside a lock
            provider_type = (await load_provider(uow, provider_id)).provider_type
        secret = _secret(provider_type, patch.api_key) if patch.api_key_set else None
        if base_url is not None:
            await check_endpoint(self._gateway, base_url, provider_type)
        now = self._clock.now()
        async with self._uow() as uow:
            provider = await load_provider(uow, provider_id, for_update=True)
            changed: list[str] = []
            if name is not None and name != provider.name:
                other = await uow.providers.get_by_name(name)
                if other is not None and other.id != provider.id:
                    raise ProviderNameTakenError(NAME_TAKEN)
                provider.name = name
                changed.append("name")
            if base_url is not None and base_url != provider.base_url:
                provider.base_url = base_url
                changed.append("base_url")
            if timeout is not None and timeout != provider.timeout_seconds:
                provider.timeout_seconds = timeout
                changed.append("timeout_seconds")
            if patch.enabled is not None and patch.enabled != provider.enabled:
                provider.enabled = patch.enabled
                changed.append("enabled")
            secret_change = await self._apply_secret(uow, provider, patch, secret)
            if "base_url" in changed or secret_change:
                # the last test said nothing about the new endpoint or key
                provider.last_test_at = provider.last_test_status = provider.last_test_error = None
            if changed or secret_change:
                provider.updated_at = now
                try:
                    await uow.providers.update(provider, at=now)
                except UniqueViolationError:
                    raise ProviderNameTakenError(NAME_TAKEN) from None
            if changed:
                await uow.audit.record(
                    by_user(
                        context,
                        client,
                        AuditAction.PROVIDER_UPDATED,
                        target_type="ai_provider",
                        target_id=provider.id,
                        details={"fields": ",".join(changed)},
                    )
                )
            if secret_change:
                await uow.audit.record(
                    by_user(
                        context,
                        client,
                        AuditAction.PROVIDER_SECRET_ROTATED,
                        target_type="ai_provider",
                        target_id=provider.id,
                        details={"change": secret_change},
                    )
                )
            await uow.commit()
        return provider

    async def _apply_secret(
        self, uow: UnitOfWork, provider: AIProvider, patch: ProviderPatch, secret: SecretValue | None
    ) -> str | None:
        if not patch.api_key_set:
            return None
        if secret is not None:
            if provider.secret_id is not None:
                record = await self._vault.replace(uow, provider.secret_id, secret)
                provider.secret_hint = record.hint
                return "replaced"
            record = await self._vault.store(uow, SecretKind.AI_API_KEY, secret)
            provider.secret_id, provider.secret_hint = record.id, record.hint
            return "added"
        if provider.secret_id is None:
            return None
        old = provider.secret_id
        provider.secret_id = provider.secret_hint = None
        await uow.providers.update(provider, at=self._clock.now())  # drop the reference first
        await uow.secrets.delete(old)
        return "removed"


class DeleteProvider:
    """Soft delete. Refused while an embedding profile uses the provider; its key and models are removed."""

    def __init__(self, uow: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def execute(self, context: SecurityContext, client: ClientInfo, provider_id: UUID) -> None:
        context.require(Permission.PROVIDERS_MANAGE)
        now = self._clock.now()
        async with self._uow() as uow:
            provider = await load_provider(uow, provider_id, for_update=True)
            if await uow.embedding_profiles.count_for_provider(provider.id):
                raise ProviderInUseError("The provider is used by an embedding profile.")
            secret_id = provider.secret_id
            provider.secret_id = provider.secret_hint = None
            provider.deleted_at = now
            await uow.providers.update(provider, at=now)
            if secret_id is not None:
                await uow.secrets.delete(secret_id)
            models = await uow.models.delete_for_provider(provider.id)
            await uow.audit.record(
                by_user(
                    context,
                    client,
                    AuditAction.PROVIDER_DELETED,
                    target_type="ai_provider",
                    target_id=provider.id,
                    details={"models_removed": models},
                )
            )
            await uow.commit()


class TestProvider:
    """Connection test (§5): lists models with the stored key. Never raises for provider failures."""

    __test__ = False  # not a pytest class

    def __init__(
        self,
        uow: UnitOfWorkFactory,
        clock: Clock,
        vault: SecretVault,
        gateway: ProviderGateway,
        limiter: RateLimiter,
    ) -> None:
        self._uow, self._clock, self._vault = uow, clock, vault
        self._gateway, self._limiter = gateway, limiter

    async def execute(
        self, context: SecurityContext, client: ClientInfo, provider_id: UUID
    ) -> ConnectionTestReport:
        context.require(Permission.PROVIDERS_MANAGE)
        async with self._uow() as uow:
            provider = await load_provider(uow, provider_id)
        await enforce_test_limit(self._limiter)
        async with self._uow() as uow:
            connection = await connection_for(uow, self._vault, provider)
        timeout = min(provider.timeout_seconds, MAX_TEST_SECONDS)
        try:
            purposes = await self._gateway.check_endpoint(provider.base_url, provider.provider_type)
        except (EndpointNotAllowed, InvalidEndpoint):
            error: ProviderError | None = ProviderEgressDeniedError()
            latency, models = 0, tuple[str, ...]()
        else:
            purpose = EgressPurpose.LLM if EgressPurpose.LLM in purposes else EgressPurpose.EMBEDDING
            result = await self._gateway.test_connection(connection, purpose=purpose, timeout=timeout)
            error, latency, models = result.error, result.latency_ms, result.models[:MAX_REPORTED_MODELS]
        status = TestStatus.OK if error is None else TestStatus.FAILED
        now = self._clock.now()
        async with self._uow() as uow:
            await load_provider(uow, provider_id, for_update=True)  # deleted meanwhile → 404
            await uow.providers.record_test(
                provider_id, at=now, status=status, error_code=str(error.code) if error else None
            )
            details: dict[str, str | int | bool] = {"status": str(status), "latency_ms": latency}
            if error is not None:
                details["error_code"] = str(error.code)
            await uow.audit.record(
                by_user(
                    context,
                    client,
                    AuditAction.PROVIDER_TESTED,
                    target_type="ai_provider",
                    target_id=provider_id,
                    details=details,
                    outcome=AuditOutcome.SUCCESS if error is None else AuditOutcome.FAILURE,
                )
            )
            await uow.commit()
        return ConnectionTestReport(
            status=status, latency_ms=latency, tested_at=now, models=models, error=error
        )


# --- chat model configs ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ModelInput:
    model_id: str
    display_name: str | None = None
    context_window: int | None = None
    max_output_tokens: int | None = None
    enabled: bool = True


@dataclass(frozen=True, slots=True)
class ModelPatch:
    """``model_id`` is immutable (another model is a new config); ``*_set`` allows clearing with ``null``."""

    display_name: str | None = None
    display_name_set: bool = False
    context_window: int | None = None
    context_window_set: bool = False
    max_output_tokens: int | None = None
    max_output_tokens_set: bool = False
    enabled: bool | None = None


async def _load_model(uow: UnitOfWork, provider_id: UUID, config_id: UUID) -> ModelConfig:
    model = await uow.models.get(provider_id, config_id)
    if model is None:
        raise NotFoundError(MODEL_NOT_FOUND)
    return model


class ListModels:
    def __init__(self, uow: UnitOfWorkFactory) -> None:
        self._uow = uow

    async def execute(self, context: SecurityContext, provider_id: UUID) -> list[ModelConfig]:
        context.require(Permission.PROVIDERS_READ)
        async with self._uow() as uow:
            await load_provider(uow, provider_id)
            return await uow.models.list_for_provider(provider_id)


class CreateModel:
    def __init__(self, uow: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def execute(
        self, context: SecurityContext, client: ClientInfo, provider_id: UUID, data: ModelInput
    ) -> ModelConfig:
        context.require(Permission.PROVIDERS_MANAGE)
        try:
            model_id = validate_model_id(data.model_id)
            display_name = normalize_display_name(data.display_name)
            context_window = validate_token_count("context_window", data.context_window)
            max_output = validate_token_count("max_output_tokens", data.max_output_tokens)
        except InvalidAIConfigError as exc:
            raise invalid(exc) from None
        now = self._clock.now()
        model = ModelConfig(
            id=new_id(),
            provider_id=provider_id,
            model_id=model_id,
            display_name=display_name,
            context_window=context_window,
            max_output_tokens=max_output,
            enabled=data.enabled,
            created_at=now,
            updated_at=now,
        )
        async with self._uow() as uow:
            await load_provider(uow, provider_id, for_update=True)
            try:
                await uow.models.add(model)
            except UniqueViolationError:
                raise ModelIdTakenError("The provider already has this model.") from None
            await uow.audit.record(
                by_user(
                    context,
                    client,
                    AuditAction.MODEL_CREATED,
                    target_type="model_config",
                    target_id=model.id,
                    details={"provider_id": str(provider_id)},
                )
            )
            await uow.commit()
        return model


class UpdateModel:
    def __init__(self, uow: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def execute(
        self,
        context: SecurityContext,
        client: ClientInfo,
        provider_id: UUID,
        config_id: UUID,
        patch: ModelPatch,
    ) -> ModelConfig:
        context.require(Permission.PROVIDERS_MANAGE)
        try:
            display_name = normalize_display_name(patch.display_name)
            context_window = validate_token_count("context_window", patch.context_window)
            max_output = validate_token_count("max_output_tokens", patch.max_output_tokens)
        except InvalidAIConfigError as exc:
            raise invalid(exc) from None
        now = self._clock.now()
        async with self._uow() as uow:
            await load_provider(uow, provider_id)
            model = await _load_model(uow, provider_id, config_id)
            changed: list[str] = []
            if patch.display_name_set and display_name != model.display_name:
                model.display_name = display_name
                changed.append("display_name")
            if patch.context_window_set and context_window != model.context_window:
                model.context_window = context_window
                changed.append("context_window")
            if patch.max_output_tokens_set and max_output != model.max_output_tokens:
                model.max_output_tokens = max_output
                changed.append("max_output_tokens")
            if patch.enabled is not None and patch.enabled != model.enabled:
                model.enabled = patch.enabled
                changed.append("enabled")
            if changed:
                model.updated_at = now
                await uow.models.update(model, at=now)
                await uow.audit.record(
                    by_user(
                        context,
                        client,
                        AuditAction.MODEL_UPDATED,
                        target_type="model_config",
                        target_id=model.id,
                        details={"fields": ",".join(changed)},
                    )
                )
            await uow.commit()
        return model


class DeleteModel:
    def __init__(self, uow: UnitOfWorkFactory) -> None:
        self._uow = uow

    async def execute(
        self, context: SecurityContext, client: ClientInfo, provider_id: UUID, config_id: UUID
    ) -> None:
        context.require(Permission.PROVIDERS_MANAGE)
        async with self._uow() as uow:
            await load_provider(uow, provider_id)
            model = await _load_model(uow, provider_id, config_id)
            await uow.models.delete(model.id)
            await uow.audit.record(
                by_user(
                    context,
                    client,
                    AuditAction.MODEL_DELETED,
                    target_type="model_config",
                    target_id=model.id,
                    details={"provider_id": str(provider_id)},
                )
            )
            await uow.commit()
