"""Embedding access for one embedding profile, shared by indexing (ADR-008 stage 9) and retrieval (§9.4).

Both sides must build vectors the same way: the configured provider of the profile, a vector of exactly the
profile's dimensions with finite values, normalized to unit length when the profile says so (ADR-017 §1).
Errors are typed so each caller maps them to its own outcome (a failed job, an API error).
"""

from __future__ import annotations

import math
from collections.abc import Sequence

from plummy.application.ai.ports import EmbeddingProvider, ProviderGateway, UnsupportedCapability
from plummy.application.errors import ServiceUnavailableError
from plummy.application.ports import UnitOfWork
from plummy.application.providers.use_cases import connection_for
from plummy.application.secret_storage.vault import SecretVault
from plummy.domain.ai import EmbeddingProfile


class EmbeddingSetupError(Exception):
    """The profile cannot produce vectors now. ``code`` is stable, ``message`` is safe to show."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


class InvalidVectorError(Exception):
    """The provider answered with a vector that does not fit the profile."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


async def embedding_provider_for(
    uow: UnitOfWork, vault: SecretVault, gateway: ProviderGateway, profile: EmbeddingProfile
) -> EmbeddingProvider:
    """The adapter of the profile's provider; the key is decrypted at the point of use."""
    provider = await uow.providers.get(profile.provider_id)
    if provider is None or provider.deleted_at is not None or not provider.enabled:
        raise EmbeddingSetupError("provider_unavailable", "The embedding provider is disabled or deleted.")
    try:
        connection = await connection_for(uow, vault, provider)
    except ServiceUnavailableError:
        raise EmbeddingSetupError("secret_unavailable", "The provider key cannot be decrypted.") from None
    try:
        return gateway.embedding(connection)
    except UnsupportedCapability:
        raise EmbeddingSetupError("capability_not_supported", "The provider has no embeddings API.") from None


def checked_vector(vector: Sequence[float], profile: EmbeddingProfile) -> tuple[float, ...]:
    """The vector size must match the profile (ADR-017 §1); ``normalize`` makes it unit length (L2)."""
    if len(vector) != profile.dimensions:
        raise InvalidVectorError(
            "embedding_dimensions_mismatch",
            f"The provider returned {len(vector)} dimensions, the profile expects {profile.dimensions}.",
        )
    values = tuple(float(value) for value in vector)
    if not all(math.isfinite(value) for value in values):
        raise InvalidVectorError("embedding_invalid_response", "The provider returned an invalid vector.")
    if profile.normalize:
        norm = math.sqrt(sum(value * value for value in values))
        if norm == 0:
            raise InvalidVectorError("embedding_invalid_response", "The provider returned a zero vector.")
        values = tuple(value / norm for value in values)
    return values
