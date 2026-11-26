"""Google OAuth pieces (ADR-020 §1-3): PKCE, ``state``, configuration and the adapter's error mapping."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from plummy.application.sources.oauth import code_challenge, new_code_verifier, new_state, plausible_state
from plummy.application.sources.ports import (
    ConnectorAuthRevokedError,
    ConnectorEgressDeniedError,
    ConnectorInvalidResponseError,
    ConnectorTransientError,
    OAuthExchangeError,
)
from plummy.composition import load_config
from plummy.core.config import ConfigurationError, load_settings
from plummy.core.instance_config import build_egress_policy, parse_instance_config
from plummy.domain.secrets import SecretValue
from plummy.infrastructure.http.safe_client import SafeHttpClient
from plummy.infrastructure.integrations.sources.google_common import GoogleEndpoints
from plummy.infrastructure.integrations.sources.google_oauth import GoogleOAuthClient
from tests.conftest import VALID_SECRET_KEY, make_settings
from tests.fake_google import CLIENT_ID, CLIENT_SECRET, FakeGoogle, connector_config
from tests.fake_http import ReceivedRequest, Reply, serve

REDIRECT = "https://plummy.example/api/v1/data-sources/oauth/callback"


def test_pkce_challenge_matches_rfc_7636_appendix_b() -> None:
    assert code_challenge("dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk") == (
        "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
    )


def test_verifier_and_state_are_random_and_well_formed() -> None:
    verifiers = {new_code_verifier() for _ in range(50)}
    assert len(verifiers) == 50
    assert all(43 <= len(value) <= 128 for value in verifiers)
    assert all(plausible_state(new_state()) for _ in range(20))


@pytest.mark.parametrize("state", [None, "", "a" * 15, "a" * 129, "a b" * 10, "ä" * 20, "a/b" * 10])
def test_implausible_states(state: str | None) -> None:
    assert not plausible_state(state)


def test_connector_section_is_parsed() -> None:
    config = parse_instance_config(connector_config(8080).encode())
    assert config.google_drive is not None
    assert config.google_drive.token_url == "http://127.0.0.1:8080/token"
    assert config.google_drive.download_hosts == frozenset()
    endpoints = GoogleEndpoints.from_config(config.google_drive)
    assert endpoints.api_url == "http://127.0.0.1:8080"
    assert GoogleEndpoints.from_config(None).download_hosts == frozenset(
        {"www.googleapis.com", "*.googleusercontent.com"}
    )


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ('[connectors.google_drive]\nauth_url = "x"', "must have exactly"),
        ("[connectors.other]\nx = 1", "may contain only"),
        (
            '[connectors.google_drive]\nauth_url = "ftp://x"\ntoken_url = "https://a.b"\n'
            'revoke_url = "https://a.b"\napi_url = "https://a.b"\ndownload_hosts = []',
            "not a valid http",
        ),
        (
            '[connectors.google_drive]\nauth_url = "https://a.b"\ntoken_url = "https://a.b"\n'
            'revoke_url = "https://a.b"\napi_url = "https://a.b"\ndownload_hosts = ["*"]',
            "download_hosts",
        ),
    ],
)
def test_invalid_connector_sections(section: str, message: str) -> None:
    with pytest.raises(ConfigurationError, match=message):
        parse_instance_config(section.encode())


def test_replacement_endpoints_are_refused_in_production(tmp_path: Path) -> None:
    path = tmp_path / "plummy.toml"
    path.write_text(connector_config(8080))
    assert load_config(make_settings(config_file=path)).google_drive is not None
    production = make_settings(
        app_env="production", config_file=path, secret_key=VALID_SECRET_KEY, session_cookie_secure=True
    )
    with pytest.raises(ConfigurationError, match="not allowed when APP_ENV=production"):
        load_config(production)


def test_client_id_and_secret_come_together() -> None:
    with pytest.raises(ConfigurationError, match="must be set together"):
        load_settings(
            app_env="test",
            database_url="postgresql+asyncpg://u:p@db/plummy",
            redis_url="redis://redis:6379/0",
            google_oauth_client_id=CLIENT_ID,
        )
    settings = make_settings(google_oauth_client_id=CLIENT_ID, google_oauth_client_secret=CLIENT_SECRET)
    assert settings.google_drive_configured
    assert settings.oauth_redirect_uri is not None
    assert settings.oauth_redirect_uri.endswith("/api/v1/data-sources/oauth/callback")
    assert CLIENT_SECRET not in repr(settings)
    assert CLIENT_SECRET not in str(settings.safe_summary())


# --- the adapter against the fake -----------------------------------------------------------------------


class _Adapter:
    def __init__(self, port: int) -> None:
        config = parse_instance_config(connector_config(port).encode())
        self.http = SafeHttpClient(build_egress_policy(config))
        self.client = GoogleOAuthClient(
            self.http,
            client_id=CLIENT_ID,
            client_secret=SecretValue(CLIENT_SECRET),
            endpoints=GoogleEndpoints.from_config(config.google_drive),
        )


async def _with_fake(google: FakeGoogle, body: Any) -> Any:
    async with serve(google.handle) as server:
        adapter = _Adapter(server.port)
        try:
            return await body(adapter.client, server)
        finally:
            await adapter.http.aclose()


async def test_exchange_refresh_and_revoke_against_the_fake() -> None:
    google = FakeGoogle()
    verifier = new_code_verifier()

    async def body(client: GoogleOAuthClient, server: Any) -> None:
        url = client.authorization_url(
            state=new_state(), code_challenge=code_challenge(verifier), redirect_uri=REDIRECT
        )
        code, _ = google.authorize(url)
        grant = await client.exchange_code(
            SecretValue(code), code_verifier=SecretValue(verifier), redirect_uri=REDIRECT
        )
        assert grant.refresh_token is not None
        assert grant.granted_scopes == ("https://www.googleapis.com/auth/drive.readonly",)
        assert client.has_required_scopes(grant.granted_scopes)
        assert not client.has_required_scopes(("https://www.googleapis.com/auth/drive.file",))
        assert await client.account_label(grant.access_token) == "Drive Bot <drive-bot@example.com>"
        refreshed = await client.refresh(grant.refresh_token)
        assert refreshed.refresh_token is not None
        assert refreshed.refresh_token.reveal() == grant.refresh_token.reveal()  # Google keeps it
        assert await client.revoke(grant.refresh_token) is True
        with pytest.raises(ConnectorAuthRevokedError):
            await client.refresh(grant.refresh_token)
        assert await client.revoke(grant.refresh_token) is True  # already revoked: not an error
        for request in server.requests:  # secrets only in bodies, never in URLs
            assert CLIENT_SECRET not in request.target
            assert grant.refresh_token.reveal() not in request.target
            assert verifier not in request.target

    await _with_fake(google, body)


async def test_wrong_verifier_and_reused_code_are_exchange_errors() -> None:
    google = FakeGoogle()
    verifier = new_code_verifier()

    async def body(client: GoogleOAuthClient, _: Any) -> None:
        url = client.authorization_url(
            state=new_state(), code_challenge=code_challenge(verifier), redirect_uri=REDIRECT
        )
        code, _ = google.authorize(url)
        with pytest.raises(OAuthExchangeError):
            await client.exchange_code(
                SecretValue(code), code_verifier=SecretValue(new_code_verifier()), redirect_uri=REDIRECT
            )
        with pytest.raises(OAuthExchangeError):  # the code was consumed by the failed attempt
            await client.exchange_code(
                SecretValue(code), code_verifier=SecretValue(verifier), redirect_uri=REDIRECT
            )

    await _with_fake(google, body)


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        (Reply.json({"error": "server_error"}, status=503), ConnectorTransientError),
        (Reply.json({"error": "invalid_request"}, status=400), OAuthExchangeError),
        (Reply(200, {"Content-Type": "application/json"}, b"not json"), ConnectorInvalidResponseError),
        (Reply.json({"token_type": "Bearer"}), ConnectorInvalidResponseError),
        (Reply.json({"access_token": "a", "refresh_token": 5}), ConnectorInvalidResponseError),
    ],
)
async def test_token_endpoint_answers_are_typed(reply: Reply, error: type[Exception]) -> None:
    async with serve(lambda _request: reply) as server:
        adapter = _Adapter(server.port)
        with pytest.raises(error):
            await adapter.client.exchange_code(
                SecretValue("code"), code_verifier=SecretValue("v" * 43), redirect_uri=REDIRECT
            )
        await adapter.http.aclose()


async def test_unlisted_internal_endpoint_is_refused() -> None:
    endpoints = GoogleEndpoints(token_url="http://127.0.0.1:9/token")
    http = SafeHttpClient(build_egress_policy(parse_instance_config(b"")))
    secret = SecretValue(CLIENT_SECRET)
    client = GoogleOAuthClient(http, client_id=CLIENT_ID, client_secret=secret, endpoints=endpoints)
    with pytest.raises(ConnectorEgressDeniedError):
        await client.refresh(SecretValue("refresh"))
    await http.aclose()


def _about(user: dict[str, Any]) -> Any:
    def handler(request: ReceivedRequest) -> Reply:
        return Reply.json({"user": user})

    return handler


@pytest.mark.parametrize(
    ("user", "label"),
    [
        ({"displayName": "A", "emailAddress": "a@example.com"}, "A <a@example.com>"),
        ({"emailAddress": "a@example.com"}, "a@example.com"),
        ({"displayName": " "}, None),
        ({}, None),
    ],
)
async def test_account_label_shapes(user: dict[str, Any], label: str | None) -> None:
    async with serve(_about(user)) as server:
        adapter = _Adapter(server.port)
        assert await adapter.client.account_label(SecretValue("access")) == label
        await adapter.http.aclose()
