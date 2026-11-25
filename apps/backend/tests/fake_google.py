"""A fake Google OAuth server for tests (ADR-020 §10), served by ``tests.fake_http.serve`` on loopback.

It behaves like the real one where security depends on it: the client secret is checked, an authorization
code works once and only with its redirect URI and the PKCE verifier of its challenge (S256), a revoked
refresh token is refused with ``invalid_grant``. Tests read what it received to prove what was (not) sent.
"""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

from tests.fake_http import ReceivedRequest, Reply

CLIENT_ID = "test-client.apps.googleusercontent.com"
CLIENT_SECRET = "test-client-secret"
DRIVE_READONLY = "https://www.googleapis.com/auth/drive.readonly"


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


@dataclass
class _Code:
    challenge: str
    redirect_uri: str
    client_id: str


@dataclass
class FakeGoogle:
    granted_scope: str = DRIVE_READONLY
    issue_refresh_token: bool = True
    user: dict[str, str] = field(
        default_factory=lambda: {"displayName": "Drive Bot", "emailAddress": "drive-bot@example.com"}
    )
    codes: dict[str, _Code] = field(default_factory=dict)
    refresh_tokens: set[str] = field(default_factory=set)
    access_tokens: set[str] = field(default_factory=set)
    revoked: list[str] = field(default_factory=list)
    token_requests: list[dict[str, str]] = field(default_factory=list)
    token_status: int | None = None  # force an answer of the token endpoint
    revoke_status: int | None = None  # force an answer of the revoke endpoint

    def authorize(self, authorization_url: str) -> tuple[str, str]:
        """What the consent screen does: checks the request and returns ``(code, state)``."""
        query = {key: values[0] for key, values in parse_qs(urlsplit(authorization_url).query).items()}
        assert query["response_type"] == "code"
        assert query["code_challenge_method"] == "S256"
        assert query["access_type"] == "offline"
        code = "code-" + secrets.token_urlsafe(16)
        self.codes[code] = _Code(query["code_challenge"], query["redirect_uri"], query["client_id"])
        return code, query["state"]

    def _form(self, request: ReceivedRequest) -> dict[str, str]:
        return {key: values[0] for key, values in parse_qs(request.body.decode()).items()}

    def _tokens(self, *, with_refresh: bool) -> Reply:
        access = "access-" + secrets.token_urlsafe(16)
        self.access_tokens.add(access)
        body: dict[str, Any] = {
            "access_token": access,
            "expires_in": 3599,
            "scope": self.granted_scope,
            "token_type": "Bearer",
        }
        if with_refresh:
            refresh = "refresh-" + secrets.token_urlsafe(24)
            self.refresh_tokens.add(refresh)
            body["refresh_token"] = refresh
        return Reply.json(body)

    def _token(self, request: ReceivedRequest) -> Reply:
        form = self._form(request)
        self.token_requests.append(form)
        if self.token_status is not None:
            return Reply.json({"error": "server_error"}, status=self.token_status)
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
            return self._tokens(with_refresh=self.issue_refresh_token)
        if form.get("grant_type") == "refresh_token":
            if form.get("refresh_token") not in self.refresh_tokens:
                return Reply.json({"error": "invalid_grant"}, status=400)
            return self._tokens(with_refresh=False)
        return Reply.json({"error": "unsupported_grant_type"}, status=400)

    def _revoke(self, request: ReceivedRequest) -> Reply:
        token = self._form(request).get("token", "")
        if self.revoke_status is not None:
            return Reply(self.revoke_status)
        self.revoked.append(token)
        if token in self.refresh_tokens:
            self.refresh_tokens.discard(token)
            return Reply(200)
        if token in self.access_tokens:
            self.access_tokens.discard(token)
            return Reply(200)
        return Reply.json({"error": "invalid_token"}, status=400)

    def _bearer(self, request: ReceivedRequest) -> bool:
        header = request.headers.get("authorization", "")
        return header.startswith("Bearer ") and header[7:] in self.access_tokens

    def handle(self, request: ReceivedRequest) -> Reply:
        path = request.target.split("?", 1)[0]
        if request.method == "POST" and path == "/token":
            return self._token(request)
        if request.method == "POST" and path == "/revoke":
            return self._revoke(request)
        if path == "/drive/v3/about":
            if not self._bearer(request):
                return Reply.json({"error": {"code": 401}}, status=401)
            return Reply.json({"user": self.user})
        return Reply.json({"error": {"code": 404}}, status=404)


def connector_config(port: int) -> str:
    """CONFIG_FILE of an app that talks to the fake on loopback (test environment only)."""
    base = f"http://127.0.0.1:{port}"
    return f"""
[[network.allowlist]]
name = "fake-google"
url = "{base}"
purposes = ["connector"]
provider_types = ["google_drive"]
allowed_cidrs = ["127.0.0.1/32"]

[connectors.google_drive]
auth_url = "{base}/o/oauth2/v2/auth"
token_url = "{base}/token"
revoke_url = "{base}/revoke"
api_url = "{base}"
download_hosts = []
"""
