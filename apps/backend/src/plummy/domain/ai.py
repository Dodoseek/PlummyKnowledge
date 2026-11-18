"""AI providers, chat model configs and embedding profiles (DATABASE_SCHEMA.md §4.2-4.4).

Pure data and validation rules; no I/O. Secrets are referenced by id only (ADR-016).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final
from uuid import UUID


class ProviderType(StrEnum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    OLLAMA = "ollama"
    OPENAI_COMPATIBLE = "openai_compatible"


KEY_REQUIRED: Final = frozenset({ProviderType.OPENAI, ProviderType.ANTHROPIC})
"""Cloud APIs reject every request without a key; local servers usually have none."""

EMBEDDING_CAPABLE: Final = frozenset(
    {ProviderType.OPENAI, ProviderType.OPENAI_COMPATIBLE, ProviderType.OLLAMA}
)

MAX_PROVIDER_NAME_LENGTH: Final = 100
MAX_MODEL_ID_LENGTH: Final = 256
MAX_DISPLAY_NAME_LENGTH: Final = 200
MIN_TIMEOUT_SECONDS: Final = 1
MAX_TIMEOUT_SECONDS: Final = 600
DEFAULT_TIMEOUT_SECONDS: Final = 60
MAX_DIMENSIONS: Final = 16000  # pgvector ``vector`` limit
MAX_TOKENS_LIMIT: Final = 10_000_000


class TestStatus(StrEnum):
    __test__ = False  # not a pytest class

    OK = "ok"
    FAILED = "failed"


class DistanceMetric(StrEnum):
    COSINE = "cosine"
    INNER_PRODUCT = "inner_product"
    L2 = "l2"


class EmbeddingProfileStatus(StrEnum):
    ACTIVE = "active"
    DEPRECATED = "deprecated"
    RETIRED = "retired"  # final: knowledge bases must move to another profile


class InvalidAIConfigError(ValueError):
    def __init__(self, field: str, code: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.code = code


@dataclass(slots=True)
class AIProvider:
    id: UUID
    name: str
    provider_type: ProviderType
    base_url: str
    secret_id: UUID | None
    secret_hint: str | None
    timeout_seconds: int
    enabled: bool
    created_at: datetime
    updated_at: datetime
    last_test_at: datetime | None = None
    last_test_status: TestStatus | None = None
    last_test_error: str | None = None  # a typed error code, never provider output
    deleted_at: datetime | None = None


@dataclass(slots=True)
class ModelConfig:
    id: UUID
    provider_id: UUID
    model_id: str
    display_name: str | None
    context_window: int | None
    max_output_tokens: int | None
    enabled: bool
    created_at: datetime
    updated_at: datetime
    kind: str = "chat"


@dataclass(slots=True)
class EmbeddingProfile:
    """Immutable except ``name`` and ``status``: another model or dimension needs a new profile + reindex."""

    id: UUID
    name: str
    provider_id: UUID
    model_id: str
    dimensions: int
    distance_metric: DistanceMetric
    normalize: bool
    max_input_tokens: int | None
    status: EmbeddingProfileStatus
    created_at: datetime
    updated_at: datetime


def _printable_single_line(value: str) -> bool:
    return value.isprintable()


def normalize_name(field: str, value: str, max_length: int = MAX_PROVIDER_NAME_LENGTH) -> str:
    name = " ".join(value.split())
    if not name or len(name) > max_length or not _printable_single_line(name):
        raise InvalidAIConfigError(
            field, "invalid_name", f"The name must be 1-{max_length} printable characters."
        )
    return name


def normalize_display_name(value: str | None) -> str | None:
    if value is None:
        return None
    name = " ".join(value.split())
    if not name:
        return None
    if len(name) > MAX_DISPLAY_NAME_LENGTH or not _printable_single_line(name):
        raise InvalidAIConfigError(
            "display_name",
            "invalid_display_name",
            f"The display name must be at most {MAX_DISPLAY_NAME_LENGTH} printable characters.",
        )
    return name


def validate_model_id(value: str) -> str:
    """The provider's model identifier: one token, no whitespace or control characters."""
    if (
        not value
        or len(value) > MAX_MODEL_ID_LENGTH
        or not value.isprintable()
        or any(char.isspace() for char in value)
    ):
        raise InvalidAIConfigError(
            "model_id",
            "invalid_model_id",
            f"The model ID must be 1-{MAX_MODEL_ID_LENGTH} characters without spaces.",
        )
    return value


def validate_timeout(value: int) -> int:
    if not MIN_TIMEOUT_SECONDS <= value <= MAX_TIMEOUT_SECONDS:
        raise InvalidAIConfigError(
            "settings.timeout_seconds",
            "invalid_timeout",
            f"The timeout must be {MIN_TIMEOUT_SECONDS}-{MAX_TIMEOUT_SECONDS} seconds.",
        )
    return value


def validate_token_count(field: str, value: int | None) -> int | None:
    if value is not None and not 1 <= value <= MAX_TOKENS_LIMIT:
        raise InvalidAIConfigError(field, "invalid_token_count", "The value must be a positive token count.")
    return value


def validate_dimensions(value: int) -> int:
    if not 1 <= value <= MAX_DIMENSIONS:
        raise InvalidAIConfigError(
            "dimensions", "invalid_dimensions", f"Dimensions must be between 1 and {MAX_DIMENSIONS}."
        )
    return value
