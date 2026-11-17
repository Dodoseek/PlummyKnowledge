"""Instance Settings (ADR-006): typed configuration from environment variables.

Sources, by priority: explicit init kwargs → environment variables → ``<NAME>_FILE`` files (Docker secrets)
for secret values. Setting both ``<NAME>`` and ``<NAME>_FILE`` is a configuration error.

Invalid configuration stops the process with a diagnostic that never contains secret values
(see :func:`load_settings`).
"""

from __future__ import annotations

import os
import secrets
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Self
from urllib.parse import urlsplit

from pydantic import Field, PrivateAttr, SecretStr, ValidationError, field_validator, model_validator
from pydantic.fields import FieldInfo
from pydantic_settings import (
    BaseSettings,
    NoDecode,
    PydanticBaseSettingsSource,
    SettingsConfigDict,
)

MIN_SECRET_KEY_LENGTH = 32

# Secret fields that may also be provided as ``<NAME>_FILE`` (path to a file with the value).
FILE_SECRET_FIELDS: tuple[str, ...] = ("database_url", "redis_url", "secret_key")


class ConfigurationError(Exception):
    """Configuration is invalid. The message is safe to print: it never contains secret values."""


class AppEnv(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class LogLevel(StrEnum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


class _FileSecretsSource(PydanticBaseSettingsSource):
    """Reads ``<NAME>_FILE`` environment variables for secret fields."""

    def get_field_value(self, field: FieldInfo, field_name: str) -> tuple[Any, str, bool]:
        # Not used: values are produced in ``__call__``.
        return None, field_name, False

    def __call__(self) -> dict[str, Any]:
        values: dict[str, Any] = {}
        for field_name in FILE_SECRET_FIELDS:
            env_name = field_name.upper()
            file_env_name = f"{env_name}_FILE"
            file_path = os.environ.get(file_env_name)
            if not file_path:
                continue
            if os.environ.get(env_name):
                raise ConfigurationError(f"{env_name} and {file_env_name} are both set; use only one of them")
            try:
                content = Path(file_path).read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise ConfigurationError(
                    f"{file_env_name}: cannot read file ({type(exc).__name__})"
                ) from None
            if not content:
                raise ConfigurationError(f"{file_env_name}: file is empty")
            values[field_name] = content
        return values


def _validate_origin(origin: str) -> str:
    parts = urlsplit(origin)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise ValueError(f"invalid origin {origin!r}: expected scheme://host[:port]")
    if parts.username or parts.password or parts.query or parts.fragment or parts.path not in {"", "/"}:
        raise ValueError(f"invalid origin {origin!r}: path, query and credentials are not allowed")
    return f"{parts.scheme}://{parts.netloc}".lower()


class Settings(BaseSettings):
    """Instance-level settings. Read-only at runtime; changes require a process restart (ADR-006)."""

    model_config = SettingsConfigDict(
        case_sensitive=False,
        extra="ignore",
        env_ignore_empty=True,
        hide_input_in_errors=True,
    )

    # Safe default: an unconfigured process gets the strictest validation.
    app_env: AppEnv = AppEnv.PRODUCTION
    app_debug: bool = False
    log_level: LogLevel = LogLevel.INFO

    secret_key: SecretStr | None = None
    database_url: SecretStr
    redis_url: SecretStr

    # Comma-separated list of allowed origins. Empty = CORS disabled (same-origin only).
    cors_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)

    db_pool_size: int = Field(default=5, ge=1, le=100)
    db_max_overflow: int = Field(default=5, ge=0, le=100)

    # Interactive API docs (Swagger UI, loads assets from a CDN). Unset = enabled except in production.
    # The OpenAPI schema (/api/v1/openapi.json) is always served.
    api_docs_enabled: bool | None = None

    _secret_key_generated: bool = PrivateAttr(default=False)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # No .env loading inside the app: Docker Compose / the process manager provides the environment.
        return init_settings, env_settings, _FileSecretsSource(settings_cls)

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: object) -> object:
        return value.upper() if isinstance(value, str) else value

    @field_validator("database_url")
    @classmethod
    def _check_database_url(cls, value: SecretStr) -> SecretStr:
        parts = urlsplit(value.get_secret_value())
        if parts.scheme != "postgresql+asyncpg":
            raise ValueError("must use the postgresql+asyncpg:// scheme")
        if not parts.hostname or parts.path in {"", "/"}:
            raise ValueError("must include a host and a database name")
        return value

    @field_validator("redis_url")
    @classmethod
    def _check_redis_url(cls, value: SecretStr) -> SecretStr:
        parts = urlsplit(value.get_secret_value())
        if parts.scheme not in {"redis", "rediss"} or not parts.hostname:
            raise ValueError("must use the redis:// or rediss:// scheme and include a host")
        return value

    @field_validator("secret_key")
    @classmethod
    def _check_secret_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value()) < MIN_SECRET_KEY_LENGTH:
            raise ValueError(f"must be at least {MIN_SECRET_KEY_LENGTH} characters long")
        return value

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _parse_cors_origins(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator("cors_origins")
    @classmethod
    def _check_cors_origins(cls, value: list[str]) -> list[str]:
        if "*" in value:
            raise ValueError("wildcard '*' is not allowed; list explicit origins")
        return list(dict.fromkeys(_validate_origin(origin) for origin in value))

    @model_validator(mode="after")
    def _check_environment_policy(self) -> Self:
        if self.app_env is AppEnv.PRODUCTION:
            if self.app_debug:
                raise ValueError("APP_DEBUG must be false when APP_ENV=production")
            if self.secret_key is None:
                raise ValueError("SECRET_KEY (or SECRET_KEY_FILE) is required when APP_ENV=production")
        elif self.secret_key is None:
            # Development/test convenience: an ephemeral key, never persisted or logged.
            self.secret_key = SecretStr(secrets.token_urlsafe(48))
            self._secret_key_generated = True
        return self

    @property
    def is_production(self) -> bool:
        return self.app_env is AppEnv.PRODUCTION

    @property
    def docs_enabled(self) -> bool:
        return self.api_docs_enabled if self.api_docs_enabled is not None else not self.is_production

    @property
    def secret_key_generated(self) -> bool:
        return self._secret_key_generated

    def safe_summary(self) -> dict[str, object]:
        """Non-secret settings for startup diagnostics."""
        return {
            "app_env": self.app_env.value,
            "app_debug": self.app_debug,
            "log_level": self.log_level.value,
            "cors_origins": self.cors_origins,
            "db_pool_size": self.db_pool_size,
            "db_max_overflow": self.db_max_overflow,
            "api_docs_enabled": self.docs_enabled,
            "secret_key_generated": self.secret_key_generated,
        }


def load_settings(**overrides: Any) -> Settings:
    """Build settings, converting validation failures into a :class:`ConfigurationError` without secrets."""
    try:
        return Settings(**overrides)
    except ValidationError as exc:
        problems = []
        for error in exc.errors(include_url=False, include_input=False, include_context=False):
            location = ".".join(str(part) for part in error["loc"]).upper() or "SETTINGS"
            problems.append(f"{location}: {error['msg']}")
        raise ConfigurationError("Invalid configuration: " + "; ".join(problems)) from None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
