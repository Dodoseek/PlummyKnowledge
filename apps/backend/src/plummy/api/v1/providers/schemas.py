"""DTOs for ``/api/v1/providers`` (API_CONTRACTS.md §2.7). API keys are write-only: never in a response."""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr

from plummy.application.pagination import Page
from plummy.application.providers.use_cases import ConnectionTestReport
from plummy.domain.ai import (
    MAX_DISPLAY_NAME_LENGTH,
    MAX_MODEL_ID_LENGTH,
    MAX_TIMEOUT_SECONDS,
    MAX_TOKENS_LIMIT,
    MIN_TIMEOUT_SECONDS,
    AIProvider,
    ModelConfig,
    ProviderType,
)
from plummy.domain.secrets import MAX_SECRET_LENGTH

MAX_NAME_INPUT_LENGTH = 200
MAX_URL_INPUT_LENGTH = 4096
MASK = "••••"


class _Request(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProviderSettings(_Request):
    timeout_seconds: int | None = Field(default=None, ge=MIN_TIMEOUT_SECONDS, le=MAX_TIMEOUT_SECONDS)


class ProviderSettingsResponse(BaseModel):
    timeout_seconds: int


class LastTest(BaseModel):
    status: Literal["ok", "failed"]
    tested_at: datetime
    error_code: str | None


class ProviderResponse(BaseModel):
    id: UUID
    name: str
    provider_type: ProviderType
    base_url: str
    has_secret: bool
    secret_hint: str | None
    enabled: bool
    settings: ProviderSettingsResponse
    last_test: LastTest | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def build(cls, provider: AIProvider) -> ProviderResponse:
        last_test = None
        if provider.last_test_at is not None and provider.last_test_status is not None:
            last_test = LastTest(
                status=provider.last_test_status.value,
                tested_at=provider.last_test_at,
                error_code=provider.last_test_error,
            )
        return cls(
            id=provider.id,
            name=provider.name,
            provider_type=provider.provider_type,
            base_url=provider.base_url,
            has_secret=provider.secret_id is not None,
            secret_hint=MASK + provider.secret_hint if provider.secret_hint else None,
            enabled=provider.enabled,
            settings=ProviderSettingsResponse(timeout_seconds=provider.timeout_seconds),
            last_test=last_test,
            created_at=provider.created_at,
            updated_at=provider.updated_at,
        )


class ProviderListResponse(BaseModel):
    items: list[ProviderResponse]
    next_cursor: str | None
    has_more: bool

    @classmethod
    def from_page(cls, page: Page[AIProvider]) -> ProviderListResponse:
        return cls(
            items=[ProviderResponse.build(item) for item in page.items],
            next_cursor=page.next_cursor,
            has_more=page.has_more,
        )


class CreateProviderRequest(_Request):
    name: str = Field(min_length=1, max_length=MAX_NAME_INPUT_LENGTH)
    provider_type: ProviderType
    base_url: str | None = Field(default=None, min_length=1, max_length=MAX_URL_INPUT_LENGTH)
    api_key: SecretStr | None = Field(default=None, max_length=MAX_SECRET_LENGTH * 2)
    settings: ProviderSettings | None = None
    enabled: bool = True


class UpdateProviderRequest(_Request):
    """``api_key``: absent → unchanged, string → replaced, ``null`` → removed. The type is immutable."""

    name: str | None = Field(default=None, min_length=1, max_length=MAX_NAME_INPUT_LENGTH)
    base_url: str | None = Field(default=None, min_length=1, max_length=MAX_URL_INPUT_LENGTH)
    api_key: SecretStr | None = Field(default=None, max_length=MAX_SECRET_LENGTH * 2)
    settings: ProviderSettings | None = None
    enabled: bool | None = None


class TestError(BaseModel):
    __test__ = False  # not a pytest class

    code: str
    message: str


class ConnectionTestResponse(BaseModel):
    status: Literal["ok", "failed"]
    latency_ms: int
    tested_at: datetime
    models: list[str]
    error: TestError | None

    @classmethod
    def build(cls, report: ConnectionTestReport) -> ConnectionTestResponse:
        error = report.error
        return cls(
            status=report.status.value,
            latency_ms=report.latency_ms,
            tested_at=report.tested_at,
            models=list(report.models),
            error=TestError(code=str(error.code), message=error.message) if error else None,
        )


class ModelResponse(BaseModel):
    id: UUID
    provider_id: UUID
    model_id: str
    display_name: str | None
    kind: str
    context_window: int | None
    max_output_tokens: int | None
    enabled: bool
    created_at: datetime
    updated_at: datetime

    @classmethod
    def build(cls, model: ModelConfig) -> ModelResponse:
        return cls(
            id=model.id,
            provider_id=model.provider_id,
            model_id=model.model_id,
            display_name=model.display_name,
            kind=model.kind,
            context_window=model.context_window,
            max_output_tokens=model.max_output_tokens,
            enabled=model.enabled,
            created_at=model.created_at,
            updated_at=model.updated_at,
        )


class ModelListResponse(BaseModel):
    items: list[ModelResponse]


class CreateModelRequest(_Request):
    model_id: str = Field(min_length=1, max_length=MAX_MODEL_ID_LENGTH)
    display_name: str | None = Field(default=None, max_length=MAX_DISPLAY_NAME_LENGTH * 2)
    context_window: int | None = Field(default=None, ge=1, le=MAX_TOKENS_LIMIT)
    max_output_tokens: int | None = Field(default=None, ge=1, le=MAX_TOKENS_LIMIT)
    enabled: bool = True


class UpdateModelRequest(_Request):
    """``model_id`` cannot change (create another model config). ``null`` clears an optional field."""

    display_name: str | None = Field(default=None, max_length=MAX_DISPLAY_NAME_LENGTH * 2)
    context_window: int | None = Field(default=None, ge=1, le=MAX_TOKENS_LIMIT)
    max_output_tokens: int | None = Field(default=None, ge=1, le=MAX_TOKENS_LIMIT)
    enabled: bool | None = None
