"""Instance Settings (ADR-006): typed configuration from environment variables.

Sources, by priority: explicit init kwargs → environment variables → ``<NAME>_FILE`` files (Docker secrets)
for secret values. Setting both ``<NAME>`` and ``<NAME>_FILE`` is a configuration error.

Invalid configuration stops the process with a diagnostic that never contains secret values
(see :func:`load_settings`).
"""

from __future__ import annotations

import os
import re
import secrets
from datetime import timedelta
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Final, Self
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

# Session lifetime bounds (ADR-013). Values outside them are configuration errors.
MIN_SESSION_IDLE_TTL = timedelta(minutes=5)
MAX_SESSION_IDLE_TTL = timedelta(days=30)
MIN_SESSION_ABSOLUTE_TTL = timedelta(hours=1)
MAX_SESSION_ABSOLUTE_TTL = timedelta(days=90)
MIN_BOOTSTRAP_TOKEN_TTL = timedelta(minutes=10)
MAX_BOOTSTRAP_TOKEN_TTL = timedelta(days=30)

DEFAULT_BOOTSTRAP_DIR = Path("/var/lib/plummy/bootstrap")
DEFAULT_STORAGE_PATH = Path("/var/lib/plummy/storage")
MIN_QUARANTINE_TTL = timedelta(minutes=10)
MAX_QUARANTINE_TTL = timedelta(days=7)

# Secret fields that may also be provided as ``<NAME>_FILE`` (path to a file with the value).
_MULTI_TENANT_ALIASES: Final = frozenset({"common", "organizations", "consumers"})
_TENANT_GUID: Final = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_TENANT_DOMAIN: Final = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}")

FILE_SECRET_FIELDS: tuple[str, ...] = (
    "database_url",
    "redis_url",
    "secret_key",
    "google_oauth_client_secret",
    "microsoft_oauth_client_secret",
)


class ConfigurationError(Exception):
    """Configuration is invalid. The message is safe to print: it never contains secret values."""


class AppEnv(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class SetupMode(StrEnum):
    """How the first administrator is created (ADR-006)."""

    WEB = "web"  # setup wizard with a bootstrap token (and the CLI)
    CLI_ONLY = "cli_only"  # web initialization is disabled; only `python -m plummy.cli admin create`


class StorageBackend(StrEnum):
    LOCAL = "local"  # S3-compatible storage arrives with the production profile (roadmap Phase 11)


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

    # Public URL of the web application (scheme://host[:port]). Its origin is trusted for state-changing
    # requests (Origin check, ADR-013). Required in production, where it must use https.
    public_url: str | None = None

    # First-run setup (ADR-006).
    setup_mode: SetupMode = SetupMode.WEB
    bootstrap_dir: Path = DEFAULT_BOOTSTRAP_DIR
    bootstrap_token_ttl: timedelta = timedelta(hours=24)

    # Sessions (ADR-013). Durations accept seconds ("86400") or ISO 8601 ("PT24H").
    session_idle_ttl: timedelta = timedelta(hours=24)
    session_absolute_ttl: timedelta = timedelta(days=7)
    # Secure cookies (`__Host-` prefix). May be false only outside production (plain-HTTP development).
    session_cookie_secure: bool = True

    # Key ring for secrets stored in the database (ADR-016): a JSON file outside the database (Docker secret
    # or a mounted volume). Required in production; elsewhere, without it, secrets cannot be saved.
    secrets_keyring_file: Path | None = None

    # Instance configuration file (TOML): the outbound network allowlist (ADR-007). Unset: empty allowlist,
    # only public https endpoints are reachable.
    config_file: Path | None = None

    # Document storage (ARCHITECTURE.md §8.5): originals and quarantine. Quarantined files older than the TTL
    # are removed by the cleanup task.
    storage_backend: StorageBackend = StorageBackend.LOCAL
    storage_path: Path = DEFAULT_STORAGE_PATH
    quarantine_ttl: timedelta = timedelta(hours=24)
    # Largest accepted upload, checked while the file streams in (SECURITY_MODEL.md §9).
    upload_max_bytes: int = Field(default=50 * 1024 * 1024, ge=1024, le=2 * 1024 * 1024 * 1024)
    # Hard limit of one answer, retrieval included (ADR-019 §2); a slower answer ends with provider_timeout.
    chat_generation_timeout_seconds: int = Field(default=120, ge=10, le=600)

    # OAuth client of the Google Drive connector (ADR-020 §2): both or neither. The redirect URI is
    # PUBLIC_URL + /api/v1/data-sources/oauth/callback; without PUBLIC_URL the connector is not configured.
    google_oauth_client_id: str | None = Field(default=None, max_length=256)
    google_oauth_client_secret: SecretStr | None = None
    # Entra ID app of the Microsoft 365 connector (ADR-021 §2): all three or none; one tenant only.
    microsoft_oauth_tenant_id: str | None = Field(default=None, max_length=256)
    microsoft_oauth_client_id: str | None = Field(default=None, max_length=256)
    microsoft_oauth_client_secret: SecretStr | None = None

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

    @field_validator("microsoft_oauth_tenant_id")
    @classmethod
    def _check_microsoft_tenant(cls, value: str | None) -> str | None:
        # One organization's directory (ADR-021 §2): a GUID or a domain; never the multi-tenant aliases.
        if value is None:
            return None
        value = value.strip().lower()
        if value in _MULTI_TENANT_ALIASES:
            raise ValueError(
                "MICROSOFT_OAUTH_TENANT_ID must be one tenant, not common/organizations/consumers"
            )
        if not (_TENANT_GUID.fullmatch(value) or _TENANT_DOMAIN.fullmatch(value)):
            raise ValueError("MICROSOFT_OAUTH_TENANT_ID must be a tenant GUID or a domain name")
        return value

    @field_validator("public_url")
    @classmethod
    def _check_public_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return _validate_origin(value.strip())

    @field_validator(
        "bootstrap_token_ttl", "session_idle_ttl", "session_absolute_ttl", "quarantine_ttl", mode="before"
    )
    @classmethod
    def _parse_seconds(cls, value: object) -> object:
        # Environment variables are strings: "86400" means seconds (ISO 8601 "PT24H" is also accepted).
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
        return value

    @field_validator("quarantine_ttl")
    @classmethod
    def _check_quarantine_ttl(cls, value: timedelta) -> timedelta:
        if not MIN_QUARANTINE_TTL <= value <= MAX_QUARANTINE_TTL:
            raise ValueError("must be between 10 minutes and 7 days")
        return value

    @field_validator("storage_path")
    @classmethod
    def _check_storage_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("must be an absolute path")
        return value

    @field_validator("bootstrap_token_ttl")
    @classmethod
    def _check_bootstrap_token_ttl(cls, value: timedelta) -> timedelta:
        if not MIN_BOOTSTRAP_TOKEN_TTL <= value <= MAX_BOOTSTRAP_TOKEN_TTL:
            raise ValueError("must be between 10 minutes and 30 days")
        return value

    @field_validator("session_idle_ttl")
    @classmethod
    def _check_session_idle_ttl(cls, value: timedelta) -> timedelta:
        if not MIN_SESSION_IDLE_TTL <= value <= MAX_SESSION_IDLE_TTL:
            raise ValueError("must be between 5 minutes and 30 days")
        return value

    @field_validator("session_absolute_ttl")
    @classmethod
    def _check_session_absolute_ttl(cls, value: timedelta) -> timedelta:
        if not MIN_SESSION_ABSOLUTE_TTL <= value <= MAX_SESSION_ABSOLUTE_TTL:
            raise ValueError("must be between 1 hour and 90 days")
        return value

    @model_validator(mode="after")
    def _check_environment_policy(self) -> Self:
        if (self.google_oauth_client_id is None) != (self.google_oauth_client_secret is None):
            raise ValueError("GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET must be set together")
        microsoft = (
            self.microsoft_oauth_tenant_id,
            self.microsoft_oauth_client_id,
            self.microsoft_oauth_client_secret,
        )
        if any(value is not None for value in microsoft) and any(value is None for value in microsoft):
            raise ValueError(
                "MICROSOFT_OAUTH_TENANT_ID, MICROSOFT_OAUTH_CLIENT_ID and MICROSOFT_OAUTH_CLIENT_SECRET "
                "must be set together"
            )
        if self.session_absolute_ttl < self.session_idle_ttl:
            raise ValueError("SESSION_ABSOLUTE_TTL must not be shorter than SESSION_IDLE_TTL")
        if self.app_env is AppEnv.PRODUCTION:
            if self.app_debug:
                raise ValueError("APP_DEBUG must be false when APP_ENV=production")
            if self.secret_key is None:
                raise ValueError("SECRET_KEY (or SECRET_KEY_FILE) is required when APP_ENV=production")
            if self.public_url is None:
                raise ValueError("PUBLIC_URL is required when APP_ENV=production")
            if not self.public_url.startswith("https://"):
                raise ValueError("PUBLIC_URL must use https when APP_ENV=production")
            if not self.session_cookie_secure:
                raise ValueError("SESSION_COOKIE_SECURE must be true when APP_ENV=production")
            if self.secrets_keyring_file is None:
                raise ValueError("SECRETS_KEYRING_FILE is required when APP_ENV=production")
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

    @property
    def google_drive_configured(self) -> bool:
        return self.google_oauth_client_id is not None and self.public_url is not None

    @property
    def microsoft_365_configured(self) -> bool:
        return self.microsoft_oauth_client_id is not None and self.public_url is not None

    @property
    def oauth_redirect_uri(self) -> str | None:
        """The exact redirect URI registered with the provider (ADR-020 §1)."""
        if self.public_url is None:
            return None
        return self.public_url.rstrip("/") + "/api/v1/data-sources/oauth/callback"

    @property
    def trusted_origins(self) -> frozenset[str]:
        """Origins allowed to send state-changing requests: PUBLIC_URL and CORS_ORIGINS (ADR-013)."""
        origins = set(self.cors_origins)
        if self.public_url is not None:
            origins.add(self.public_url)
        return frozenset(origins)

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
            "public_url": self.public_url,
            "setup_mode": self.setup_mode.value,
            "session_idle_ttl_seconds": int(self.session_idle_ttl.total_seconds()),
            "session_absolute_ttl_seconds": int(self.session_absolute_ttl.total_seconds()),
            "session_cookie_secure": self.session_cookie_secure,
            "secrets_keyring_configured": self.secrets_keyring_file is not None,
            "config_file_configured": self.config_file is not None,
            "storage_backend": self.storage_backend.value,
            "quarantine_ttl_seconds": int(self.quarantine_ttl.total_seconds()),
            "upload_max_bytes": self.upload_max_bytes,
            "google_drive_configured": self.google_drive_configured,
            "microsoft_365_configured": self.microsoft_365_configured,
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
