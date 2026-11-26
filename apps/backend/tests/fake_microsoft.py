"""A fake Microsoft identity platform and Graph for tests (ADR-021 §10), served by ``tests.fake_http.serve``
on loopback.

It behaves like the real one where security depends on it: the client secret is checked, an authorization
code works once and only with its redirect URI and the PKCE verifier of its challenge (S256), a refresh token
is rotated and the old one dies, a dead refresh token is refused with ``invalid_grant``. There is no revoke
endpoint, as in Microsoft's. Tests read what it received to prove what was (not) sent.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

from tests.fake_http import ReceivedRequest, Reply

TENANT = "contoso.onmicrosoft.com"
CLIENT_ID = "00000000-0000-4000-8000-00000000c11e"
CLIENT_SECRET = "test-entra-client-secret"
GRAPH = "https://graph.microsoft.com/"
# What Entra answers in ``scope``: Graph permissions with the resource prefix, offline_access without it.
GRANTED = f"{GRAPH}Files.Read.All {GRAPH}Sites.Read.All {GRAPH}User.Read"


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


@dataclass
class _Code:
    challenge: str
    redirect_uri: str
    client_id: str


@dataclass
class FakeMicrosoft:
    granted_scope: str = GRANTED
    issue_refresh_token: bool = True
    refresh_error: str = "invalid_grant"  # what a dead refresh token gets (or "interaction_required")
    user: dict[str, str] = field(
        default_factory=lambda: {"displayName": "Graph Bot", "userPrincipalName": "graph-bot@contoso.example"}
    )
    codes: dict[str, _Code] = field(default_factory=dict)
    refresh_tokens: set[str] = field(default_factory=set)
    access_tokens: set[str] = field(default_factory=set)
    token_requests: list[dict[str, str]] = field(default_factory=list)
    token_status: int | None = None  # force an answer of the token endpoint
    api_failures: list[Reply] = field(default_factory=list)  # answered first, one per Graph request

    def authorize(self, authorization_url: str) -> tuple[str, str]:
        """What the sign-in and consent pages do: check the request and return ``(code, state)``."""
        query = {key: values[0] for key, values in parse_qs(urlsplit(authorization_url).query).items()}
        assert query["response_type"] == "code"
        assert query["code_challenge_method"] == "S256"
        assert "offline_access" in query["scope"].split()
        code = "code-" + secrets.token_urlsafe(16)
        self.codes[code] = _Code(query["code_challenge"], query["redirect_uri"], query["client_id"])
        return code, query["state"]

    def _form(self, request: ReceivedRequest) -> dict[str, str]:
        return {key: values[0] for key, values in parse_qs(request.body.decode()).items()}

    def _tokens(self) -> Reply:
        access = "eyJ0eXAi.access-" + secrets.token_urlsafe(16)
        self.access_tokens.add(access)
        body: dict[str, Any] = {
            "token_type": "Bearer",
            "scope": self.granted_scope,
            "expires_in": 4271,
            "ext_expires_in": 4271,
            "access_token": access,
        }
        if self.issue_refresh_token:
            refresh = "1.AQ-refresh-" + secrets.token_urlsafe(24)
            self.refresh_tokens.add(refresh)
            body["refresh_token"] = refresh
        return Reply.json(body)

    def _token(self, request: ReceivedRequest) -> Reply:
        form = self._form(request)
        self.token_requests.append(form)
        if self.token_status is not None:
            return Reply.json({"error": "temporarily_unavailable"}, status=self.token_status)
        if form.get("client_id") != CLIENT_ID or form.get("client_secret") != CLIENT_SECRET:
            return Reply.json({"error": "invalid_client"}, status=401)
        if form.get("grant_type") == "authorization_code":
            issued = self.codes.pop(form.get("code", ""), None)
            if (
                issued is None
                or issued.redirect_uri != form.get("redirect_uri")
                or _challenge(form.get("code_verifier", "")) != issued.challenge
            ):
                return Reply.json({"error": "invalid_grant"}, status=400)
            return self._tokens()
        if form.get("grant_type") == "refresh_token":
            token = form.get("refresh_token", "")
            if token not in self.refresh_tokens:
                return Reply.json({"error": self.refresh_error}, status=400)
            self.refresh_tokens.discard(token)  # rotation: the old refresh token dies
            return self._tokens()
        return Reply.json({"error": "unsupported_grant_type"}, status=400)

    def revoke_all(self) -> None:
        """What an administrator does in Entra ("revoke sessions"): every refresh token dies."""
        self.refresh_tokens.clear()

    def _bearer(self, request: ReceivedRequest) -> bool:
        header = request.headers.get("authorization", "")
        return header.startswith("Bearer ") and header[7:] in self.access_tokens

    def _graph(self, request: ReceivedRequest, path: str) -> Reply:
        if not self._bearer(request):
            return Reply.json({"error": {"code": "InvalidAuthenticationToken"}}, status=401)
        if self.api_failures:
            return self.api_failures.pop(0)
        if path == "me":
            return Reply.json(self.user)
        return Reply.json({"error": {"code": "itemNotFound"}}, status=404)

    def handle(self, request: ReceivedRequest) -> Reply:
        path = urlsplit(request.target).path
        if request.method == "POST" and path == f"/{TENANT}/oauth2/v2.0/token":
            return self._token(request)
        if request.method == "GET" and path.startswith("/v1.0/"):
            return self._graph(request, path.removeprefix("/v1.0/"))
        return Reply.json({"error": {"code": "itemNotFound"}}, status=404)


def connector_config(port: int) -> str:
    """CONFIG_FILE of an app that talks to the fake on loopback (test environment only)."""
    base = f"http://127.0.0.1:{port}"
    return f"""
[[network.allowlist]]
name = "fake-microsoft"
url = "{base}"
purposes = ["connector"]
provider_types = ["microsoft_365"]
allowed_cidrs = ["127.0.0.1/32"]

[connectors.microsoft_365]
auth_url = "{base}/{TENANT}/oauth2/v2.0/authorize"
token_url = "{base}/{TENANT}/oauth2/v2.0/token"
api_url = "{base}/v1.0"
download_hosts = []
"""
