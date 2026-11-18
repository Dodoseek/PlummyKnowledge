"""Embedding profiles (API_CONTRACTS.md §2.8, DATABASE_SCHEMA.md §4.4).

A profile fixes the vector space of a knowledge base: provider, model, dimensions, metric and normalization
never change. Changing any of them answers ``409 reindex_required``: create a new profile and reindex.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final
from uuid import UUID

from plummy.application.ai.ports import (
    CancellationToken,
    ProviderError,
    ProviderGateway,
    UnsupportedCapability,
)
from plummy.application.audit import by_user
from plummy.application.errors import (
    EmbeddingProfileNameTakenError,
    InvalidInputError,
    InvalidStateError,
    NotFoundError,
    ReindexRequiredError,
)
from plummy.application.pagination import Page, PageRequest
from plummy.application.ports import Clock, RateLimiter, UniqueViolationError, UnitOfWork, UnitOfWorkFactory
from plummy.application.providers.use_cases import (
    connection_for,
    enforce_test_limit,
    invalid,
    load_provider,
)
from plummy.application.secret_storage.vault import SecretVault
from plummy.application.security import ClientInfo, SecurityContext
from plummy.core.ids import new_id
from plummy.domain.ai import (
    EMBEDDING_CAPABLE,
    DistanceMetric,
    EmbeddingProfile,
    EmbeddingProfileStatus,
    InvalidAIConfigError,
    TestStatus,
    normalize_name,
    validate_dimensions,
    validate_model_id,
    validate_token_count,
)
from plummy.domain.audit import AuditAction
from plummy.domain.identity import Permission

PROFILE_NOT_FOUND: Final = "Embedding profile not found."
NAME_TAKEN: Final = "An embedding profile with this name already exists."
TEST_INPUT: Final = "PlummyKnowledge embedding profile test."
IMMUTABLE_FIELDS: Final = ("provider_id", "model_id", "dimensions", "distance_metric", "normalize")


@dataclass(frozen=True, slots=True)
class ProfileInput:
    name: str
    provider_id: UUID
    model_id: str
    dimensions: int
    distance_metric: DistanceMetric = DistanceMetric.COSINE
    normalize: bool = False
    max_input_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class ProfilePatch:
    name: str | None = None
    status: EmbeddingProfileStatus | None = None
    # Immutable fields, accepted only to answer 409 reindex_required when they differ.
    provider_id: UUID | None = None
    model_id: str | None = None
    dimensions: int | None = None
    distance_metric: DistanceMetric | None = None
    normalize: bool | None = None


@dataclass(frozen=True, slots=True)
class ProfileTestReport:
    status: TestStatus
    latency_ms: int
    tested_at: datetime
    expected_dimensions: int
    actual_dimensions: int | None = None
    error_code: str | None = None
    error_message: str | None = None


async def _load(uow: UnitOfWork, profile_id: UUID) -> EmbeddingProfile:
    profile = await uow.embedding_profiles.get(profile_id)
    if profile is None:
        raise NotFoundError(PROFILE_NOT_FOUND)
    return profile


class ListEmbeddingProfiles:
    def __init__(self, uow: UnitOfWorkFactory) -> None:
        self._uow = uow

    async def execute(self, context: SecurityContext, page: PageRequest) -> Page[EmbeddingProfile]:
        context.require(Permission.PROVIDERS_READ)
        async with self._uow() as uow:
            return await uow.embedding_profiles.page(page)


class GetEmbeddingProfile:
    def __init__(self, uow: UnitOfWorkFactory) -> None:
        self._uow = uow

    async def execute(self, context: SecurityContext, profile_id: UUID) -> EmbeddingProfile:
        context.require(Permission.PROVIDERS_READ)
        async with self._uow() as uow:
            return await _load(uow, profile_id)


class CreateEmbeddingProfile:
    def __init__(self, uow: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def execute(
        self, context: SecurityContext, client: ClientInfo, data: ProfileInput
    ) -> EmbeddingProfile:
        context.require(Permission.PROVIDERS_MANAGE)
        try:
            name = normalize_name("name", data.name)
            model_id = validate_model_id(data.model_id)
            dimensions = validate_dimensions(data.dimensions)
            max_input = validate_token_count("max_input_tokens", data.max_input_tokens)
        except InvalidAIConfigError as exc:
            raise invalid(exc) from None
        now = self._clock.now()
        profile = EmbeddingProfile(
            id=new_id(),
            name=name,
            provider_id=data.provider_id,
            model_id=model_id,
            dimensions=dimensions,
            distance_metric=data.distance_metric,
            normalize=data.normalize,
            max_input_tokens=max_input,
            status=EmbeddingProfileStatus.ACTIVE,
            created_at=now,
            updated_at=now,
        )
        async with self._uow() as uow:
            provider = await uow.providers.get_for_update(data.provider_id)
            if provider is None:
                raise InvalidInputError("provider_id", "unknown_provider", "The provider does not exist.")
            if provider.provider_type not in EMBEDDING_CAPABLE:
                raise InvalidInputError(
                    "provider_id", "capability_not_supported", "This provider type has no embeddings API."
                )
            if await uow.embedding_profiles.get_by_name(name) is not None:
                raise EmbeddingProfileNameTakenError(NAME_TAKEN)
            try:
                await uow.embedding_profiles.add(profile)
            except UniqueViolationError:
                raise EmbeddingProfileNameTakenError(NAME_TAKEN) from None
            await uow.audit.record(
                by_user(
                    context,
                    client,
                    AuditAction.EMBEDDING_PROFILE_CREATED,
                    target_type="embedding_profile",
                    target_id=profile.id,
                    details={"provider_id": str(provider.id), "dimensions": dimensions},
                )
            )
            await uow.commit()
        return profile


class UpdateEmbeddingProfile:
    def __init__(self, uow: UnitOfWorkFactory, clock: Clock) -> None:
        self._uow, self._clock = uow, clock

    async def execute(
        self, context: SecurityContext, client: ClientInfo, profile_id: UUID, patch: ProfilePatch
    ) -> EmbeddingProfile:
        context.require(Permission.PROVIDERS_MANAGE)
        try:
            name = normalize_name("name", patch.name) if patch.name is not None else None
        except InvalidAIConfigError as exc:
            raise invalid(exc) from None
        now = self._clock.now()
        async with self._uow() as uow:
            profile = await _load(uow, profile_id)
            for field_name in IMMUTABLE_FIELDS:
                requested = getattr(patch, field_name)
                if requested is not None and requested != getattr(profile, field_name):
                    raise ReindexRequiredError(
                        "The model and vector settings of a profile cannot change. "
                        "Create a new profile and reindex the knowledge bases."
                    )
            changed: list[str] = []
            if name is not None and name != profile.name:
                other = await uow.embedding_profiles.get_by_name(name)
                if other is not None and other.id != profile.id:
                    raise EmbeddingProfileNameTakenError(NAME_TAKEN)
                profile.name = name
                changed.append("name")
            if patch.status is not None and patch.status != profile.status:
                if profile.status is EmbeddingProfileStatus.RETIRED:
                    raise InvalidStateError("A retired profile cannot be reactivated.")
                profile.status = patch.status
                changed.append("status")
            if changed:
                profile.updated_at = now
                try:
                    await uow.embedding_profiles.update(profile, at=now)
                except UniqueViolationError:
                    raise EmbeddingProfileNameTakenError(NAME_TAKEN) from None
                await uow.audit.record(
                    by_user(
                        context,
                        client,
                        AuditAction.EMBEDDING_PROFILE_UPDATED,
                        target_type="embedding_profile",
                        target_id=profile.id,
                        details={"fields": ",".join(changed), "status": str(profile.status)},
                    )
                )
            await uow.commit()
        return profile


class TestEmbeddingProfile:
    """Embeds one short text and compares the vector size with the profile (API_CONTRACTS.md §2.8)."""

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

    async def execute(self, context: SecurityContext, profile_id: UUID) -> ProfileTestReport:
        context.require(Permission.PROVIDERS_MANAGE)
        async with self._uow() as uow:
            profile = await _load(uow, profile_id)
            provider = await load_provider(uow, profile.provider_id)
        await enforce_test_limit(self._limiter)
        async with self._uow() as uow:
            connection = await connection_for(uow, self._vault, provider)
        started = self._clock.now()
        try:
            embedder = self._gateway.embedding(connection)
            vectors = await embedder.embed(
                [TEST_INPUT], model_id=profile.model_id, cancel=CancellationToken()
            )
        except UnsupportedCapability:
            return self._report(
                profile,
                started,
                error_code="capability_not_supported",
                message="This provider type has no embeddings API.",
            )
        except ProviderError as exc:
            return self._report(profile, started, error_code=str(exc.code), message=exc.message)
        actual = len(vectors[0])
        if actual != profile.dimensions:
            return self._report(
                profile,
                started,
                actual=actual,
                error_code="dimension_mismatch",
                message="The model returned vectors of another size than the profile declares.",
            )
        return self._report(profile, started, actual=actual)

    def _report(
        self,
        profile: EmbeddingProfile,
        started: datetime,
        *,
        actual: int | None = None,
        error_code: str | None = None,
        message: str | None = None,
    ) -> ProfileTestReport:
        now = self._clock.now()
        return ProfileTestReport(
            status=TestStatus.OK if error_code is None else TestStatus.FAILED,
            latency_ms=max(0, round((now - started).total_seconds() * 1000)),
            tested_at=now,
            expected_dimensions=profile.dimensions,
            actual_dimensions=actual,
            error_code=error_code,
            error_message=message,
        )
