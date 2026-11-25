"""A fake Google OAuth and Drive v3 server for tests (ADR-020 §10), served by ``tests.fake_http.serve`` on
loopback.

It behaves like the real one where security depends on it: the client secret is checked, an authorization
code works once and only with its redirect URI and the PKCE verifier of its challenge (S256), a revoked
refresh token is refused with ``invalid_grant``. Tests read what it received to prove what was (not) sent.
"""

from __future__ import annotations

import base64
import hashlib
import itertools
import re
import secrets
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from tests.fake_http import ReceivedRequest, Reply

CLIENT_ID = "test-client.apps.googleusercontent.com"
CLIENT_SECRET = "test-client-secret"
DRIVE_READONLY = "https://www.googleapis.com/auth/drive.readonly"
FOLDER = "application/vnd.google-apps.folder"
_PARENT = re.compile(r"'((?:[^'\\]|\\.)*)' in parents")
_ids = itertools.count(1)


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
    rotate_refresh_tokens: bool = False  # a refresh answers with a new refresh token (the old one dies)
    # Drive: resources by id (keys starting with "_" are private to the fake), contents, the change log
    files: dict[str, dict[str, Any]] = field(default_factory=dict)
    contents: dict[str, bytes] = field(default_factory=dict)
    shared_drives: list[dict[str, str]] = field(default_factory=list)
    changes: list[dict[str, Any]] = field(default_factory=list)
    api_failures: list[Reply] = field(default_factory=list)  # answered first, one per Drive API request
    download_redirect: str | None = None  # alt=media answers 302 to this base URL

    # --- Drive tree -----------------------------------------------------------------------------------------

    def add(
        self,
        name: str,
        *,
        parent: str = "root",
        mime: str = "text/plain",
        content: bytes | None = None,
        shared: bool = False,
        **extra: Any,
    ) -> str:
        item_id = f"id{next(_ids)}x" + secrets.token_hex(4)
        resource: dict[str, Any] = {
            "id": item_id,
            "name": name,
            "mimeType": mime,
            "parents": [parent],
            "trashed": False,
            "modifiedTime": "2026-11-26T10:00:00.000Z",
            "_shared": shared,
            **extra,
        }
        if content is not None:
            self.contents[item_id] = content
            if not mime.startswith("application/vnd.google-apps."):
                resource["size"] = str(len(content))
                resource["md5Checksum"] = hashlib.md5(content).hexdigest()  # noqa: S324 - as Drive does
            else:
                resource["version"] = "1"
        self.files[item_id] = resource
        return item_id

    def folder(self, name: str, *, parent: str = "root", shared: bool = False) -> str:
        return self.add(name, parent=parent, mime=FOLDER, shared=shared)

    def change(self, item_id: str, *, removed: bool = False) -> None:
        self.changes.append({"fileId": item_id, "removed": removed})

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
            if self.rotate_refresh_tokens:
                self.refresh_tokens.discard(form["refresh_token"])
            return self._tokens(with_refresh=self.rotate_refresh_tokens)
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

    @staticmethod
    def _public(resource: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in resource.items() if not key.startswith("_")}

    @staticmethod
    def _page(items: list[Any], query: dict[str, str], key: str, *, size: int = 100) -> dict[str, Any]:
        start = int(query.get("pageToken", "0"))
        size = int(query.get("pageSize", str(size)))
        body: dict[str, Any] = {key: items[start : start + size]}
        if start + size < len(items):
            body["nextPageToken"] = str(start + size)
        return body

    def _list(self, query: dict[str, str]) -> Reply:
        q = query.get("q", "")
        folders_only = f"mimeType = '{FOLDER}'" in q
        match = _PARENT.search(q)
        if match is not None:
            parent = match.group(1).replace("\\'", "'")
            known = (
                parent == "root" or parent in self.files or any(d["id"] == parent for d in self.shared_drives)
            )
            if not known:
                return Reply.json({"error": {"code": 404, "errors": [{"reason": "notFound"}]}}, status=404)
        found = []
        for resource in self.files.values():
            if resource["trashed"] or (folders_only and resource["mimeType"] != FOLDER):
                continue
            if "sharedWithMe = true" in q:
                if "root" not in resource["parents"] and not resource["_shared"]:
                    continue
            elif match is None or match.group(1).replace("\\'", "'") not in resource["parents"]:
                continue
            found.append(self._public(resource))
        found.sort(key=lambda item: item["name"])
        return Reply.json(self._page(found, query, "files"))

    def _changes(self, query: dict[str, str]) -> Reply:
        token = query.get("pageToken", "")
        if not token.isdigit() or int(token) > len(self.changes):
            return Reply.json({"error": {"code": 400, "message": "Invalid Value"}}, status=400)
        start, size = int(token), int(query.get("pageSize", "100"))
        changes = []
        for entry in self.changes[start : start + size]:
            change: dict[str, Any] = {
                "changeType": "file",
                "fileId": entry["fileId"],
                "removed": entry["removed"],
            }
            resource = self.files.get(entry["fileId"])
            if not entry["removed"] and resource is not None:
                change["file"] = self._public(resource)
            changes.append(change)
        body: dict[str, Any] = {"changes": changes}
        if start + size < len(self.changes):
            body["nextPageToken"] = str(start + size)
        else:
            body["newStartPageToken"] = str(len(self.changes))
        return Reply.json(body)

    def _drive(self, request: ReceivedRequest, path: str, query: dict[str, str]) -> Reply:
        if not self._bearer(request):
            return Reply.json({"error": {"code": 401}}, status=401)
        if self.api_failures:
            return self.api_failures.pop(0)
        if path == "about":
            return Reply.json({"user": self.user})
        if path == "drives":
            return Reply.json({"drives": self.shared_drives})
        if path == "files":
            return self._list(query)
        if path == "changes/startPageToken":
            return Reply.json({"startPageToken": str(len(self.changes))})
        if path == "changes":
            return self._changes(query)
        parts = path.split("/")
        if parts[0] == "files" and len(parts) in (2, 3):
            item_id = unquote(parts[1])
            resource = self.files.get(item_id)
            drive = next((drive for drive in self.shared_drives if drive["id"] == item_id), None)
            if resource is None and drive is not None:  # the root folder of a shared drive
                resource = {"id": item_id, "name": drive["name"], "mimeType": FOLDER, "driveId": item_id}
            if resource is None:
                return Reply.json({"error": {"code": 404}}, status=404)
            if len(parts) == 3 and parts[2] == "export":
                return Reply(200, {"Content-Type": query["mimeType"]}, self.contents.get(item_id, b""))
            if query.get("alt") == "media":
                if self.download_redirect is not None:
                    return Reply(302, {"Location": self.download_redirect + "/content/" + item_id})
                return Reply(200, {"Content-Type": resource["mimeType"]}, self.contents.get(item_id, b""))
            return Reply.json(self._public(resource))
        return Reply.json({"error": {"code": 404}}, status=404)

    def handle(self, request: ReceivedRequest) -> Reply:
        parts = urlsplit(request.target)
        path = parts.path
        if request.method == "POST" and path == "/token":
            return self._token(request)
        if request.method == "POST" and path == "/revoke":
            return self._revoke(request)
        if request.method == "GET" and path.startswith("/drive/v3/"):
            query = {key: values[0] for key, values in parse_qs(parts.query).items()}
            return self._drive(request, path.removeprefix("/drive/v3/"), query)
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
