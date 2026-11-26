"""Microsoft 365 OAuth pieces (ADR-021 §2, §3, §7): Instance Settings, the ``[connectors.microsoft_365]``
section, scope normalization and the adapter against a fake Entra ID on loopback."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from plummy.application.sources.oauth import code_challenge, new_code_verifier, new_state
from plummy.application.sources.ports import (
    ConnectorAccessTokenRejectedError,
    ConnectorAuthRevokedError,
    ConnectorEgressDeniedError,
    ConnectorInvalidResponseError,
    ConnectorNotFoundError,
    ConnectorPermissionDeniedError,
    ConnectorQuotaExceededError,
    ConnectorRateLimitedError,
    ConnectorTransientError,
    OAuthExchangeError,
)
from plummy.composition import load_config, oauth_clients
from plummy.core.config import ConfigurationError, load_settings
from plummy.core.instance_config import build_egress_policy, parse_instance_config
from plummy.domain.knowledge import DataSourceType
from plummy.domain.secrets import SecretValue
from plummy.infrastructure.http.safe_client import SafeHttpClient
from plummy.infrastructure.integrations.sources.microsoft_common import (
    MicrosoftEndpoints,
    api_error,
    normalize_scope,
)
from plummy.infrastructure.integrations.sources.microsoft_oauth import MicrosoftOAuthClient
from tests.conftest import VALID_SECRET_KEY, make_settings
from tests.fake_http import ReceivedRequest, Reply, serve
from tests.fake_microsoft import CLIENT_ID, CLIENT_SECRET, TENANT, FakeMicrosoft, connector_config

REDIRECT = "https://plummy.example/api/v1/data-sources/oauth/callback"
GUID = "72f988bf-86f1-41af-91ab-2d7cd011db47"

# --- Instance Settings ----------------------------------------------------------------------------------


def _load(**values: Any) -> Any:
    return load_settings(
        app_env="test",
        database_url="postgresql+asyncpg://u:p@db/plummy",
        redis_url="redis://redis:6379/0",
        **values,
    )


@pytest.mark.parametrize(
    "values",
    [
        {"microsoft_oauth_client_id": CLIENT_ID},
        {"microsoft_oauth_tenant_id": TENANT, "microsoft_oauth_client_id": CLIENT_ID},
        {"microsoft_oauth_tenant_id": TENANT, "microsoft_oauth_client_secret": CLIENT_SECRET},
        {"microsoft_oauth_client_id": CLIENT_ID, "microsoft_oauth_client_secret": CLIENT_SECRET},
    ],
)
def test_tenant_client_id_and_secret_come_together(values: dict[str, str]) -> None:
    with pytest.raises(ConfigurationError, match="must be set together"):
        _load(**values)


@pytest.mark.parametrize("tenant", ["common", "organizations", "consumers", "Common", " consumers "])
def test_multi_tenant_authorities_are_refused(tenant: str) -> None:
    with pytest.raises(ConfigurationError, match="one tenant"):
        _load(
            microsoft_oauth_tenant_id=tenant,
            microsoft_oauth_client_id=CLIENT_ID,
            microsoft_oauth_client_secret=CLIENT_SECRET,
        )


@pytest.mark.parametrize("tenant", ["contoso", "../common", "a b.com", "x" * 300, "", "72f988bf-86f1"])
def test_malformed_tenants_are_refused(tenant: str) -> None:
    with pytest.raises(ConfigurationError, match="MICROSOFT_OAUTH_TENANT_ID"):
        _load(
            microsoft_oauth_tenant_id=tenant,
            microsoft_oauth_client_id=CLIENT_ID,
            microsoft_oauth_client_secret=CLIENT_SECRET,
        )


@pytest.mark.parametrize("tenant", [GUID, GUID.upper(), TENANT, "corp.example.com"])
def test_a_single_tenant_is_accepted_and_the_secret_stays_hidden(tenant: str) -> None:
    settings = make_settings(
        microsoft_oauth_tenant_id=tenant,
        microsoft_oauth_client_id=CLIENT_ID,
        microsoft_oauth_client_secret=CLIENT_SECRET,
    )
    assert settings.microsoft_oauth_tenant_id == tenant.lower()
    assert settings.microsoft_365_configured
    assert not settings.google_drive_configured
    assert CLIENT_SECRET not in repr(settings)
    assert settings.safe_summary()["microsoft_365_configured"] is True
    assert CLIENT_SECRET not in str(settings.safe_summary())


def test_not_configured_without_the_values() -> None:
    settings = make_settings()
    assert not settings.microsoft_365_configured
    assert DataSourceType.MICROSOFT_365 not in oauth_clients(
        settings, SafeHttpClient(build_egress_policy(parse_instance_config(b"")))
    )


def test_the_secret_can_come_from_a_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    secret_file = tmp_path / "secret"
    secret_file.write_text(CLIENT_SECRET + "\n")
    monkeypatch.setenv("MICROSOFT_OAUTH_CLIENT_SECRET_FILE", str(secret_file))
    settings = _load(microsoft_oauth_tenant_id=TENANT, microsoft_oauth_client_id=CLIENT_ID)
    assert settings.microsoft_oauth_client_secret is not None
    assert settings.microsoft_oauth_client_secret.get_secret_value() == CLIENT_SECRET


# --- endpoints and CONFIG_FILE --------------------------------------------------------------------------


def test_the_authority_is_the_configured_tenant() -> None:
    endpoints = MicrosoftEndpoints.for_tenant(GUID)
    assert endpoints.auth_url == f"https://login.microsoftonline.com/{GUID}/oauth2/v2.0/authorize"
    assert endpoints.token_url == f"https://login.microsoftonline.com/{GUID}/oauth2/v2.0/token"
    assert endpoints.api_url == "https://graph.microsoft.com/v1.0"
    assert endpoints.download_hosts == frozenset({"graph.microsoft.com", "*.sharepoint.com"})


def test_connector_section_is_parsed() -> None:
    config = parse_instance_config(connector_config(8080).encode())
    assert config.microsoft_365 is not None
    assert config.google_drive is None
    endpoints = MicrosoftEndpoints.for_tenant(TENANT, config.microsoft_365)
    assert endpoints.token_url == f"http://127.0.0.1:8080/{TENANT}/oauth2/v2.0/token"
    assert endpoints.api_url == "http://127.0.0.1:8080/v1.0"
    assert endpoints.download_hosts == frozenset()


@pytest.mark.parametrize(
    ("section", "message"),
    [
        ('[connectors.microsoft_365]\nauth_url = "https://a.b"', "must have exactly"),
        (  # Microsoft has no revoke endpoint: the key of the Google section is refused
            '[connectors.microsoft_365]\nauth_url = "https://a.b"\ntoken_url = "https://a.b"\n'
            'revoke_url = "https://a.b"\napi_url = "https://a.b"\ndownload_hosts = []',
            "must have exactly",
        ),
        (
            '[connectors.microsoft_365]\nauth_url = "ftp://x"\ntoken_url = "https://a.b"\n'
            'api_url = "https://a.b"\ndownload_hosts = []',
            "not a valid http",
        ),
        (
            '[connectors.microsoft_365]\nauth_url = "https://a.b"\ntoken_url = "https://a.b"\n'
            'api_url = "https://a.b"\ndownload_hosts = ["*"]',
            "download_hosts",
        ),
        ("[connectors.sharepoint]\nx = 1", "may contain only"),
    ],
)
def test_invalid_connector_sections(section: str, message: str) -> None:
    with pytest.raises(ConfigurationError, match=message):
        parse_instance_config(section.encode())


def test_replacement_endpoints_are_refused_in_production(tmp_path: Path) -> None:
    path = tmp_path / "plummy.toml"
    path.write_text(connector_config(8080))
    assert load_config(make_settings(config_file=path)).microsoft_365 is not None
    production = make_settings(
        app_env="production", config_file=path, secret_key=VALID_SECRET_KEY, session_cookie_secure=True
    )
    with pytest.raises(ConfigurationError, match=r"\[connectors.microsoft_365\] is not allowed"):
        load_config(production)


# --- scopes ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("scope", "normalized"),
    [
        ("https://graph.microsoft.com/Files.Read.All", "files.read.all"),
        ("HTTPS://GRAPH.MICROSOFT.COM/Sites.Read.All", "sites.read.all"),
        ("Files.Read.All", "files.read.all"),
        ("offline_access", "offline_access"),
        (
            "https://graph.microsoft.com.evil/Files.Read.All",
            "https://graph.microsoft.com.evil/files.read.all",
        ),
    ],
)
def test_scopes_are_normalized(scope: str, normalized: str) -> None:
    assert normalize_scope(scope) == normalized


def _client(http: SafeHttpClient, endpoints: MicrosoftEndpoints | None = None) -> MicrosoftOAuthClient:
    return MicrosoftOAuthClient(
        http,
        client_id=CLIENT_ID,
        client_secret=SecretValue(CLIENT_SECRET),
        endpoints=endpoints or MicrosoftEndpoints.for_tenant(TENANT),
    )


@pytest.mark.parametrize(
    ("granted", "enough"),
    [
        (("https://graph.microsoft.com/Files.Read.All", "https://graph.microsoft.com/Sites.Read.All"), True),
        (("files.read.all", "SITES.READ.ALL", "User.Read", "offline_access"), True),
        (("https://graph.microsoft.com/Files.Read.All", "User.Read"), False),
        (("https://graph.microsoft.com/Files.Read", "https://graph.microsoft.com/Sites.Read.All"), False),
        (("https://graph.microsoft.com/Sites.Selected", "https://graph.microsoft.com/Files.Read.All"), False),
        ((), False),
    ],
)
def test_required_scopes(granted: tuple[str, ...], enough: bool) -> None:
    client = _client(SafeHttpClient(build_egress_policy(parse_instance_config(b""))))
    assert client.has_required_scopes(granted) is enough


def test_authorization_url_asks_one_tenant_with_pkce_and_offline_access() -> None:
    client = _client(SafeHttpClient(build_egress_policy(parse_instance_config(b""))))
    verifier = new_code_verifier()
    url = client.authorization_url(
        state="s" * 32, code_challenge=code_challenge(verifier), redirect_uri=REDIRECT
    )
    parts = urlsplit(url)
    assert (parts.scheme, parts.netloc, parts.path) == (
        "https",
        "login.microsoftonline.com",
        f"/{TENANT}/oauth2/v2.0/authorize",
    )
    query = {key: values[0] for key, values in parse_qs(parts.query).items()}
    assert query["client_id"] == CLIENT_ID
    assert query["redirect_uri"] == REDIRECT
    assert query["response_type"] == "code"
    assert query["code_challenge_method"] == "S256"
    assert query["code_challenge"] == code_challenge(verifier)
    assert query["prompt"] == "select_account"
    assert query["scope"].split() == [
        "offline_access",
        "https://graph.microsoft.com/Files.Read.All",
        "https://graph.microsoft.com/Sites.Read.All",
        "https://graph.microsoft.com/User.Read",
    ]
    assert verifier not in url
    assert CLIENT_SECRET not in url


# --- the adapter against the fake -----------------------------------------------------------------------


class _Adapter:
    def __init__(self, port: int) -> None:
        config = parse_instance_config(connector_config(port).encode())
        self.http = SafeHttpClient(build_egress_policy(config))
        self.client = _client(self.http, MicrosoftEndpoints.for_tenant(TENANT, config.microsoft_365))


async def _with_fake(microsoft: FakeMicrosoft, body: Any) -> Any:
    async with serve(microsoft.handle) as server:
        adapter = _Adapter(server.port)
        try:
            return await body(adapter.client, server)
        finally:
            await adapter.http.aclose()


async def _grant(microsoft: FakeMicrosoft, client: MicrosoftOAuthClient) -> Any:
    verifier = new_code_verifier()
    url = client.authorization_url(
        state=new_state(), code_challenge=code_challenge(verifier), redirect_uri=REDIRECT
    )
    code, _ = microsoft.authorize(url)
    return await client.exchange_code(
        SecretValue(code), code_verifier=SecretValue(verifier), redirect_uri=REDIRECT
    )


async def test_exchange_and_refresh_with_rotation_against_the_fake() -> None:
    microsoft = FakeMicrosoft()

    async def body(client: MicrosoftOAuthClient, server: Any) -> None:
        grant = await _grant(microsoft, client)
        assert grant.refresh_token is not None
        assert client.has_required_scopes(grant.granted_scopes)
        assert grant.expires_in_seconds == 4271
        assert await client.account_label(grant.access_token) == "Graph Bot <graph-bot@contoso.example>"
        refreshed = await client.refresh(grant.refresh_token)
        assert refreshed.refresh_token is not None
        assert refreshed.refresh_token.reveal() != grant.refresh_token.reveal()  # rotated
        with pytest.raises(ConnectorAuthRevokedError):  # the old one died with the rotation
            await client.refresh(grant.refresh_token)
        assert (await client.refresh(refreshed.refresh_token)).access_token.reveal()
        for request in server.requests:  # secrets only in bodies, never in URLs
            assert CLIENT_SECRET not in request.target
            assert grant.refresh_token.reveal() not in request.target
        assert all(form["client_secret"] == CLIENT_SECRET for form in microsoft.token_requests)

    await _with_fake(microsoft, body)


async def test_revoke_reports_false_without_calling_microsoft() -> None:
    microsoft = FakeMicrosoft()

    async def body(client: MicrosoftOAuthClient, server: Any) -> None:
        grant = await _grant(microsoft, client)
        before = len(server.requests)
        assert await client.revoke(grant.refresh_token) is False
        assert len(server.requests) == before

    await _with_fake(microsoft, body)


@pytest.mark.parametrize("error", ["invalid_grant", "interaction_required"])
async def test_a_dead_refresh_token_means_revoked(error: str) -> None:
    microsoft = FakeMicrosoft(refresh_error=error)

    async def body(client: MicrosoftOAuthClient, _: Any) -> None:
        grant = await _grant(microsoft, client)
        microsoft.revoke_all()
        with pytest.raises(ConnectorAuthRevokedError):
            await client.refresh(grant.refresh_token)

    await _with_fake(microsoft, body)


async def test_wrong_verifier_and_reused_code_are_exchange_errors() -> None:
    microsoft = FakeMicrosoft()
    verifier = new_code_verifier()

    async def body(client: MicrosoftOAuthClient, _: Any) -> None:
        url = client.authorization_url(
            state=new_state(), code_challenge=code_challenge(verifier), redirect_uri=REDIRECT
        )
        code, _ = microsoft.authorize(url)
        with pytest.raises(OAuthExchangeError):
            await client.exchange_code(
                SecretValue(code), code_verifier=SecretValue(new_code_verifier()), redirect_uri=REDIRECT
            )
        with pytest.raises(OAuthExchangeError):
            await client.exchange_code(
                SecretValue(code), code_verifier=SecretValue(verifier), redirect_uri=REDIRECT
            )

    await _with_fake(microsoft, body)


@pytest.mark.parametrize(
    ("reply", "error"),
    [
        (Reply.json({"error": "temporarily_unavailable"}, status=503), ConnectorTransientError),
        (Reply.json({"error": "invalid_request"}, status=400), OAuthExchangeError),
        (Reply.json({"error": "invalid_client"}, status=401), OAuthExchangeError),
        (Reply(200, {"Content-Type": "application/json"}, b"not json"), ConnectorInvalidResponseError),
        (Reply.json({"token_type": "Bearer"}), ConnectorInvalidResponseError),
        (Reply.json({"access_token": "a" * 20000}), ConnectorInvalidResponseError),
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


async def test_grant_without_refresh_token_or_scope_is_returned_as_is() -> None:
    reply = Reply.json({"access_token": "a", "expires_in": "3599"})
    async with serve(lambda _request: reply) as server:
        adapter = _Adapter(server.port)
        grant = await adapter.client.exchange_code(
            SecretValue("code"), code_verifier=SecretValue("v" * 43), redirect_uri=REDIRECT
        )
        await adapter.http.aclose()
    assert (grant.refresh_token, grant.granted_scopes, grant.expires_in_seconds) == (None, (), 3599)


async def test_the_real_authority_is_not_reachable_without_the_allowlist() -> None:
    http = SafeHttpClient(build_egress_policy(parse_instance_config(b"")))
    endpoints = MicrosoftEndpoints(auth_url="http://127.0.0.1:9/a", token_url="http://127.0.0.1:9/token")
    with pytest.raises(ConnectorEgressDeniedError):
        await _client(http, endpoints).refresh(SecretValue("refresh"))
    await http.aclose()


@pytest.mark.parametrize(
    ("user", "label"),
    [
        ({"displayName": "A", "userPrincipalName": "a@contoso.example"}, "A <a@contoso.example>"),
        ({"userPrincipalName": "a@contoso.example"}, "a@contoso.example"),
        ({"displayName": "A"}, "A"),
        ({"displayName": " ", "userPrincipalName": 5}, None),
    ],
)
async def test_account_label_shapes(user: dict[str, Any], label: str | None) -> None:
    async with serve(lambda _request: Reply.json(user)) as server:
        adapter = _Adapter(server.port)
        assert await adapter.client.account_label(SecretValue("access")) == label
        await adapter.http.aclose()


def _reply(status: int, code: str = "", **headers: str) -> httpx.Response:
    body = {"error": {"code": code, "message": "a message with a file name.docx"}} if code else {}
    return httpx.Response(status, json=body, headers=headers)


@pytest.mark.parametrize(
    ("status", "code", "error"),
    [
        (401, "InvalidAuthenticationToken", ConnectorAccessTokenRejectedError),
        (403, "accessDenied", ConnectorPermissionDeniedError),
        (403, "quotaLimitReached", ConnectorQuotaExceededError),
        (507, "insufficientStorage", ConnectorQuotaExceededError),
        (404, "itemNotFound", ConnectorNotFoundError),
        (429, "activityLimitReached", ConnectorRateLimitedError),
        (503, "serviceNotAvailable", ConnectorRateLimitedError),
        (500, "generalException", ConnectorTransientError),
        (504, "", ConnectorTransientError),
        (400, "invalidRequest", ConnectorInvalidResponseError),
    ],
)
def test_graph_errors_are_typed_without_their_text(status: int, code: str, error: type[Exception]) -> None:
    mapped = api_error(_reply(status, code))
    assert isinstance(mapped, error)
    assert "docx" not in str(mapped)


def test_retry_after_is_kept_for_throttling() -> None:
    mapped = api_error(_reply(429, "activityLimitReached", **{"Retry-After": "17"}))
    assert isinstance(mapped, ConnectorRateLimitedError)
    assert mapped.retry_after_seconds == 17.0


async def test_account_label_errors_are_typed() -> None:
    def handler(_request: ReceivedRequest) -> Reply:
        return Reply.json({"error": {"code": "InvalidAuthenticationToken"}}, status=401)

    async with serve(handler) as server:
        adapter = _Adapter(server.port)
        with pytest.raises(ConnectorAccessTokenRejectedError):
            await adapter.client.account_label(SecretValue("access"))
        await adapter.http.aclose()
