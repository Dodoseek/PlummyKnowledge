from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from plummy.core.config import AppEnv, ConfigurationError, LogLevel, get_settings, load_settings
from tests.conftest import (
    DB_PASSWORD,
    TEST_KEYRING_PATH,
    TEST_PUBLIC_URL,
    VALID_DATABASE_URL,
    VALID_REDIS_URL,
    VALID_SECRET_KEY,
    make_settings,
)

pytestmark = pytest.mark.usefixtures("clean_env")


def _set_required_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", VALID_DATABASE_URL)
    monkeypatch.setenv("REDIS_URL", VALID_REDIS_URL)
    monkeypatch.setenv("PUBLIC_URL", TEST_PUBLIC_URL)
    monkeypatch.setenv("SECRETS_KEYRING_FILE", TEST_KEYRING_PATH)


# --- positive -------------------------------------------------------------------------------------------


def test_reads_settings_from_environment(clean_env: pytest.MonkeyPatch) -> None:
    _set_required_env(clean_env)
    clean_env.setenv("APP_ENV", "development")
    clean_env.setenv("LOG_LEVEL", "debug")
    clean_env.setenv("CORS_ORIGINS", "http://localhost:5173, https://kb.example.com/")
    clean_env.setenv("DB_POOL_SIZE", "10")

    settings = get_settings()

    assert settings.app_env is AppEnv.DEVELOPMENT
    assert settings.log_level is LogLevel.DEBUG
    assert settings.cors_origins == ["http://localhost:5173", "https://kb.example.com"]
    assert settings.db_pool_size == 10
    assert settings.database_url.get_secret_value() == VALID_DATABASE_URL


def test_defaults_are_secure(clean_env: pytest.MonkeyPatch) -> None:
    _set_required_env(clean_env)
    clean_env.setenv("SECRET_KEY", VALID_SECRET_KEY)

    settings = load_settings()

    assert settings.app_env is AppEnv.PRODUCTION  # unconfigured → strictest mode
    assert settings.app_debug is False
    assert settings.cors_origins == []  # CORS closed
    assert settings.log_level is LogLevel.INFO
    assert settings.docs_enabled is False  # Swagger UI (public CDN assets) off in production


def test_api_docs_flag_is_read_from_environment(clean_env: pytest.MonkeyPatch) -> None:
    _set_required_env(clean_env)
    clean_env.setenv("SECRET_KEY", VALID_SECRET_KEY)
    clean_env.setenv("API_DOCS_ENABLED", "true")

    settings = load_settings()

    assert settings.is_production
    assert settings.docs_enabled is True
    assert settings.safe_summary()["api_docs_enabled"] is True


def test_api_docs_are_enabled_by_default_outside_production() -> None:
    assert make_settings(app_env="development").docs_enabled is True
    assert make_settings(app_env="development", api_docs_enabled=False).docs_enabled is False


def test_invalid_api_docs_flag_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="API_DOCS_ENABLED"):
        make_settings(api_docs_enabled="sometimes")


def test_production_accepts_valid_configuration() -> None:
    settings = make_settings(app_env="production", secret_key=VALID_SECRET_KEY)
    assert settings.is_production
    assert settings.secret_key_generated is False


@pytest.mark.parametrize("env", ["development", "test"])
def test_non_production_generates_ephemeral_secret_key(env: str) -> None:
    first = make_settings(app_env=env)
    second = make_settings(app_env=env)

    assert first.secret_key_generated is True
    assert first.secret_key is not None
    assert second.secret_key is not None
    assert len(first.secret_key.get_secret_value()) >= 32
    assert first.secret_key.get_secret_value() != second.secret_key.get_secret_value()


def test_empty_secret_key_env_is_treated_as_unset(clean_env: pytest.MonkeyPatch) -> None:
    _set_required_env(clean_env)
    clean_env.setenv("APP_ENV", "development")
    clean_env.setenv("SECRET_KEY", "")
    assert load_settings().secret_key_generated is True


def test_secret_values_are_read_from_files(clean_env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    db_file = tmp_path / "db_url"
    db_file.write_text(VALID_DATABASE_URL + "\n", encoding="utf-8")
    key_file = tmp_path / "secret_key"
    key_file.write_text(VALID_SECRET_KEY, encoding="utf-8")
    clean_env.setenv("DATABASE_URL_FILE", str(db_file))
    clean_env.setenv("SECRET_KEY_FILE", str(key_file))
    clean_env.setenv("REDIS_URL", VALID_REDIS_URL)
    clean_env.setenv("PUBLIC_URL", TEST_PUBLIC_URL)
    clean_env.setenv("SECRETS_KEYRING_FILE", TEST_KEYRING_PATH)

    settings = load_settings()

    assert settings.database_url.get_secret_value() == VALID_DATABASE_URL  # trailing newline stripped
    assert settings.secret_key is not None
    assert settings.secret_key.get_secret_value() == VALID_SECRET_KEY


def test_rediss_scheme_is_accepted() -> None:
    assert make_settings(redis_url="rediss://:pw@redis.internal:6380/1").redis_url


def test_cors_origins_are_deduplicated_and_normalized() -> None:
    settings = make_settings(
        cors_origins="HTTPS://KB.example.com,https://kb.example.com,http://localhost:5173"
    )
    assert settings.cors_origins == ["https://kb.example.com", "http://localhost:5173"]


# --- secrets never leak ---------------------------------------------------------------------------------


def test_secrets_are_masked_in_repr_and_summary() -> None:
    settings = make_settings(secret_key=VALID_SECRET_KEY)
    rendered = repr(settings) + str(settings.safe_summary())
    assert DB_PASSWORD not in rendered
    assert VALID_SECRET_KEY not in rendered
    assert "database_url" not in settings.safe_summary()


def test_validation_error_message_does_not_contain_secret_values() -> None:
    bad_url = f"mysql://root:{DB_PASSWORD}@db/plummy"
    with pytest.raises(ConfigurationError) as exc_info:
        make_settings(database_url=bad_url, secret_key="short-but-secret-value")
    message = str(exc_info.value)
    assert "DATABASE_URL" in message
    assert "SECRET_KEY" in message
    assert DB_PASSWORD not in message
    assert "short-but-secret-value" not in message
    assert exc_info.value.__cause__ is None


# --- negative -------------------------------------------------------------------------------------------


def test_missing_required_settings_fail(clean_env: pytest.MonkeyPatch) -> None:
    with pytest.raises(ConfigurationError, match=r"DATABASE_URL: Field required.*REDIS_URL: Field required"):
        load_settings()


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://user:pw@db/plummy",  # sync driver
        "postgresql+psycopg://user:pw@db/plummy",
        "postgresql+asyncpg://user:pw@db",  # no database name
        "postgresql+asyncpg:///plummy",  # no host
        "not a url",
    ],
)
def test_invalid_database_url_is_rejected(url: str) -> None:
    with pytest.raises(ConfigurationError, match="DATABASE_URL"):
        make_settings(database_url=url)


@pytest.mark.parametrize("url", ["http://redis:6379", "redis://", "localhost:6379"])
def test_invalid_redis_url_is_rejected(url: str) -> None:
    with pytest.raises(ConfigurationError, match="REDIS_URL"):
        make_settings(redis_url=url)


def test_production_requires_secret_key() -> None:
    with pytest.raises(ConfigurationError, match="SECRET_KEY"):
        make_settings(app_env="production")


def test_production_requires_secrets_keyring_file() -> None:
    with pytest.raises(ConfigurationError, match="SECRETS_KEYRING_FILE is required"):
        make_settings(app_env="production", secret_key=VALID_SECRET_KEY, secrets_keyring_file=None)


@pytest.mark.parametrize("role", ["ingestion", "scheduler", "migrate"])
def test_processes_without_secrets_run_in_production_without_them(role: str) -> None:
    """ADR-023 §3: ingestion, the scheduler and migrate run without a key ring, SECRET_KEY or PUBLIC_URL."""
    settings = make_settings(
        app_env="production", process_role=role, secret_key=None, secrets_keyring_file=None, public_url=None
    )
    assert settings.is_production
    assert settings.safe_summary()["process_role"] == role


@pytest.mark.parametrize("role", ["api", "worker"])
@pytest.mark.parametrize(
    ("missing", "message"),
    [
        ({"secret_key": None}, "SECRET_KEY"),
        ({"secrets_keyring_file": None}, "SECRETS_KEYRING_FILE is required"),
        ({"public_url": None}, "PUBLIC_URL is required"),
    ],
)
def test_api_and_worker_need_their_secrets_in_production(
    role: str, missing: dict[str, object], message: str
) -> None:
    with pytest.raises(ConfigurationError, match=message):
        make_settings(
            **{"app_env": "production", "process_role": role, "secret_key": VALID_SECRET_KEY, **missing}
        )


@pytest.mark.parametrize("role", ["api", "ingestion"])
def test_plain_http_public_url_is_refused_in_production_for_every_role(role: str) -> None:
    with pytest.raises(ConfigurationError, match="PUBLIC_URL must use https"):
        make_settings(
            app_env="production",
            process_role=role,
            secret_key=VALID_SECRET_KEY,
            public_url="http://x.example",
        )


def test_unknown_process_role_is_refused() -> None:
    with pytest.raises(ConfigurationError):
        make_settings(process_role="frontend")


def test_secrets_keyring_file_is_optional_outside_production() -> None:
    settings = make_settings(app_env="development")
    assert settings.secrets_keyring_file is None
    assert settings.safe_summary()["secrets_keyring_configured"] is False


def test_production_forbids_debug() -> None:
    with pytest.raises(ConfigurationError, match="APP_DEBUG"):
        make_settings(app_env="production", app_debug=True, secret_key=VALID_SECRET_KEY)


def test_debug_is_allowed_outside_production() -> None:
    assert make_settings(app_env="development", app_debug=True).app_debug is True


@pytest.mark.parametrize("length", [1, 31])
def test_short_secret_key_is_rejected(length: int) -> None:
    with pytest.raises(ConfigurationError, match=r"SECRET_KEY: .*at least 32"):
        make_settings(secret_key="k" * length)


def test_secret_key_of_minimum_length_is_accepted() -> None:
    assert make_settings(secret_key="k" * 32).secret_key is not None


@pytest.mark.parametrize(
    "origins",
    [
        "*",
        "http://localhost:5173,*",
        "localhost:5173",
        "ftp://example.com",
        "https://example.com/app",
        "https://example.com?x=1",
        "https://user:pw@example.com",
    ],
)
def test_invalid_cors_origins_are_rejected(origins: str) -> None:
    with pytest.raises(ConfigurationError, match="CORS_ORIGINS"):
        make_settings(cors_origins=origins)


@pytest.mark.parametrize("env", ["staging", "prod", ""])
def test_unknown_app_env_is_rejected(env: str) -> None:
    with pytest.raises(ConfigurationError, match="APP_ENV"):
        make_settings(app_env=env)


def test_unknown_log_level_is_rejected() -> None:
    with pytest.raises(ConfigurationError, match="LOG_LEVEL"):
        make_settings(log_level="verbose")


@pytest.mark.parametrize(
    ("field", "value"), [("db_pool_size", 0), ("db_pool_size", 101), ("db_max_overflow", -1)]
)
def test_pool_bounds_are_enforced(field: str, value: int) -> None:
    with pytest.raises(ConfigurationError, match=field.upper()):
        make_settings(**{field: value})


def test_value_and_file_together_are_rejected(clean_env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    secret_file = tmp_path / "db_url"
    secret_file.write_text(VALID_DATABASE_URL, encoding="utf-8")
    _set_required_env(clean_env)
    clean_env.setenv("DATABASE_URL_FILE", str(secret_file))
    with pytest.raises(ConfigurationError, match="DATABASE_URL and DATABASE_URL_FILE are both set"):
        load_settings()


def test_unreadable_secret_file_is_rejected(clean_env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    clean_env.setenv("REDIS_URL", VALID_REDIS_URL)
    clean_env.setenv("DATABASE_URL_FILE", str(tmp_path / "missing"))
    with pytest.raises(ConfigurationError, match="DATABASE_URL_FILE: cannot read file"):
        load_settings()


def test_empty_secret_file_is_rejected(clean_env: pytest.MonkeyPatch, tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.write_text("  \n", encoding="utf-8")
    clean_env.setenv("REDIS_URL", VALID_REDIS_URL)
    clean_env.setenv("DATABASE_URL_FILE", str(empty))
    with pytest.raises(ConfigurationError, match="DATABASE_URL_FILE: file is empty"):
        load_settings()


def test_storage_defaults(clean_env: pytest.MonkeyPatch) -> None:
    _set_required_env(clean_env)
    clean_env.setenv("SECRET_KEY", VALID_SECRET_KEY)
    settings = load_settings()
    assert settings.storage_backend.value == "local"
    assert settings.storage_path == Path("/var/lib/plummy/storage")
    assert settings.quarantine_ttl == timedelta(hours=24)


def test_storage_settings_are_read_from_environment(clean_env: pytest.MonkeyPatch) -> None:
    _set_required_env(clean_env)
    clean_env.setenv("STORAGE_PATH", "/srv/plummy/storage")
    clean_env.setenv("QUARANTINE_TTL", "600")
    settings = load_settings(app_env="test")
    assert settings.storage_path == Path("/srv/plummy/storage")
    assert settings.quarantine_ttl == timedelta(minutes=10)


@pytest.mark.parametrize("ttl", ["599", "604801", "PT1M"])
def test_quarantine_ttl_bounds(ttl: str) -> None:
    with pytest.raises(ConfigurationError, match="QUARANTINE_TTL"):
        make_settings(quarantine_ttl=ttl)


@pytest.mark.parametrize(
    ("field", "value"), [("storage_path", "relative/storage"), ("storage_backend", "s3")]
)
def test_invalid_storage_settings_are_rejected(field: str, value: str) -> None:
    with pytest.raises(ConfigurationError, match=field.upper()):
        make_settings(**{field: value})


def test_database_role_settings(clean_env: pytest.MonkeyPatch) -> None:
    _set_required_env(clean_env)
    clean_env.setenv("DATABASE_RUNTIME_ROLE", "plummy_app")
    clean_env.setenv("AUDIT_RETENTION_DAYS", "30")
    settings = load_settings(app_env="test")
    assert settings.database_runtime_role == "plummy_app"
    assert settings.audit_retention_days == 30
    clean_env.delenv("DATABASE_RUNTIME_ROLE")
    clean_env.delenv("AUDIT_RETENTION_DAYS")
    assert make_settings().database_runtime_role is None
    assert make_settings().audit_retention_days == 365


@pytest.mark.parametrize("role", ['plummy"; DROP ROLE x; --', "Plummy", "1app", "", "a" * 64, "app-role"])
def test_a_runtime_role_that_is_not_a_plain_name_is_rejected(role: str) -> None:
    with pytest.raises(ConfigurationError, match="DATABASE_RUNTIME_ROLE"):
        make_settings(database_runtime_role=role)


@pytest.mark.parametrize("days", [0, 29, -1, 36501])
def test_audit_retention_bounds(days: int) -> None:
    with pytest.raises(ConfigurationError, match="AUDIT_RETENTION_DAYS"):
        make_settings(audit_retention_days=days)


def test_production_cors_origins_must_use_https() -> None:
    with pytest.raises(ConfigurationError) as exc_info:
        make_settings(app_env="production", secret_key=VALID_SECRET_KEY, cors_origins="http://kb.example.com")
    assert "CORS_ORIGINS must use https" in str(exc_info.value)
    settings = make_settings(
        app_env="production", secret_key=VALID_SECRET_KEY, cors_origins="https://kb.example.com"
    )
    assert settings.trusted_origins == {"https://kb.example.com", TEST_PUBLIC_URL}
    # Outside production a local frontend dev server is allowed.
    assert make_settings(cors_origins="http://localhost:5173").cors_origins == ["http://localhost:5173"]


def test_general_rate_limit_setting() -> None:
    assert make_settings().rate_limit_general_per_minute == 300
    assert make_settings(rate_limit_general_per_minute=1000).rate_limit_general_per_minute == 1000
    for invalid in (0, 9, 100_001):
        with pytest.raises(ConfigurationError):
            make_settings(rate_limit_general_per_minute=invalid)
    assert make_settings().safe_summary()["rate_limit_general_per_minute"] == 300
