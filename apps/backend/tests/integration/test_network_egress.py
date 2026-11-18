"""Outbound network policy in the application (ADR-007): audit, Instance Settings view, startup checks."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from plummy.composition import build_resources
from plummy.core.config import ConfigurationError
from plummy.core.network.errors import EgressDeniedError
from plummy.core.network.policy import EgressPurpose
from tests.fake_http import Reply, serve
from tests.integration.harness import Harness, fast_hasher, harness_settings, running_auth_app
from tests.integration.rbac import setup_admin_client

pytestmark = pytest.mark.integration

LOOPBACK_CONFIG = """
[[network.allowlist]]
name = "local-model"
url = "http://127.0.0.1:{port}"
purposes = ["llm"]
provider_types = ["ollama"]
allowed_cidrs = ["127.0.0.1/32"]
"""


async def test_denied_connection_is_audited_without_path_or_query(harness: Harness) -> None:
    with pytest.raises(EgressDeniedError):
        await harness.resources.http.request(
            "GET",
            "https://169.254.169.254/latest/meta-data/iam?token=sk-secret",
            purpose=EgressPurpose.LLM,
            provider_type="openai",
        )
    rows = await harness.sql(
        "SELECT actor_type, outcome, details FROM audit_logs WHERE action = 'network.egress.denied'"
    )
    assert len(rows) == 1
    actor_type, outcome, details = rows[0]
    assert (actor_type, outcome) == ("system", "denied")
    assert details == {
        "purpose": "llm",
        "provider_type": "openai",
        "scheme": "https",
        "host": "169.254.169.254",
        "port": 443,
        "reason": "address_never_allowed",
    }
    rendered = json.dumps(details)
    assert "meta-data" not in rendered
    assert "sk-secret" not in rendered


async def test_allowlisted_endpoint_is_reachable_and_shown_in_settings(
    migrated_database_url: str, redis_url: str, clean_rate_limits: None, tmp_path: Path
) -> None:
    async with serve(lambda _: Reply.json({"models": []})) as server:
        config = tmp_path / "plummy.toml"
        config.write_text(LOOPBACK_CONFIG.format(port=server.port))
        async with running_auth_app(
            migrated_database_url, redis_url, tmp_path / "bootstrap", config_file=config
        ) as app:
            response = await app.resources.http.request(
                "GET",
                f"http://127.0.0.1:{server.port}/api/tags",
                purpose=EgressPurpose.LLM,
                provider_type="ollama",
            )
            assert response.json() == {"models": []}
            with pytest.raises(EgressDeniedError):  # same address, other provider type (SC-06)
                await app.resources.http.request(
                    "GET",
                    f"http://127.0.0.1:{server.port}/",
                    purpose=EgressPurpose.LLM,
                    provider_type="openai",
                )

            admin = await setup_admin_client(app)
            settings = (await admin.get("/api/v1/system/settings")).json()
    assert settings["network"] == {
        "allowlist": [
            {
                "name": "local-model",
                "url": f"http://127.0.0.1:{server.port}",
                "purposes": ["llm"],
                "provider_types": ["ollama"],
                "allowed_cidrs": ["127.0.0.1/32"],
            }
        ]
    }
    assert len(server.requests) == 1


async def test_settings_show_an_empty_allowlist_by_default(harness: Harness) -> None:
    admin = await setup_admin_client(harness)
    body = (await admin.get("/api/v1/system/settings")).json()
    assert body["network"] == {"allowlist": []}


async def test_allowlist_overlapping_the_database_stops_startup(
    migrated_database_url: str, redis_url: str, tmp_path: Path
) -> None:
    from sqlalchemy import make_url

    database_host = make_url(migrated_database_url).host
    assert database_host is not None
    database_ip = socket.getaddrinfo(database_host, None, family=socket.AF_INET)[0][4][0]
    config = tmp_path / "plummy.toml"
    config.write_text(
        f"""
[[network.allowlist]]
name = "too-wide"
url = "http://models.internal:8080"
purposes = ["llm"]
allowed_cidrs = ["{database_ip}/32"]
"""
    )
    settings = harness_settings(migrated_database_url, redis_url, tmp_path / "bootstrap", config_file=config)
    with pytest.raises(ConfigurationError, match="includes the address of PostgreSQL"):
        build_resources(settings, password_hasher=fast_hasher())


def test_check_configuration_fails_for_an_invalid_config_file(
    clean_env: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from plummy.main import check_configuration

    config = tmp_path / "plummy.toml"
    config.write_text('[[network.allowlist]]\nname = "x"\nurl = "http://user:pw@svc/"\npurposes = ["llm"]\n')
    clean_env.setenv("APP_ENV", "development")
    clean_env.setenv("DATABASE_URL", "postgresql+asyncpg://plummy:x@db/plummy")
    clean_env.setenv("REDIS_URL", "redis://redis:6379/0")
    clean_env.setenv("CONFIG_FILE", str(config))
    assert check_configuration() == 1
