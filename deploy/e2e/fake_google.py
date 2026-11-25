"""Fake Google OAuth and Drive v3 for the end-to-end tests (compose.e2e.yaml, ADR-020). Test infrastructure
only.

Standard library only, no network access. The backend reaches it through ``[connectors.google_drive]`` and the
allowlist of ``deploy/e2e/plummy.toml``; the browser opens its consent page, which approves at once:

- ``GET /o/oauth2/v2/auth`` checks the request (client ID, exact redirect URI, ``code`` + S256 PKCE, offline
  access) and answers ``302`` to the redirect URI with a one-time code and the ``state``;
- ``POST /token`` exchanges a code (client secret, redirect URI and PKCE verifier checked) or a refresh token;
  ``POST /revoke`` revokes a token;
- ``GET /drive/v3/...`` serves ``about``, ``drives``, ``files`` (list, get, ``alt=media``) and ``changes``
  for a fixed tree: the folder ``E2E Sales`` (``pricing.md``, ``notes.txt``) and ``outside.txt`` next to it;
- ``POST /_control/remove`` with ``{"name": …}`` deletes a file as if in Drive (a ``removed`` change);
  ``GET /_control/tokens`` lists every token it issued, so a test can prove none of them reached the browser.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

CLIENT_ID = os.environ.get("FAKE_GOOGLE_CLIENT_ID", "e2e-client.apps.googleusercontent.com")
CLIENT_SECRET = os.environ.get("FAKE_GOOGLE_CLIENT_SECRET", "")
REDIRECT_URI = os.environ.get(
    "FAKE_GOOGLE_REDIRECT_URI", "http://localhost:5173/api/v1/data-sources/oauth/callback"
)
SCOPE = "https://www.googleapis.com/auth/drive.readonly"
FOLDER = "application/vnd.google-apps.folder"
PARENT = re.compile(r"'((?:[^'\\]|\\.)*)' in parents")
MAX_BODY = 64 * 1024

PRICING = b"""# Partner pricing

The partner discount code is Quillon-4412. Partners get the discount for annual plans only.
"""
NOTES = b"Sales notes: the quarterly review is in April.\n"
OUTSIDE = b"Outside the synced folder: Marrowind-0937.\n"

_lock = threading.Lock()
_codes: dict[str, tuple[str, str]] = {}  # code -> (PKCE challenge, redirect URI)
_refresh: set[str] = set()
_access: set[str] = set()
_issued: list[str] = []
_files: dict[str, dict[str, Any]] = {}
_contents: dict[str, bytes] = {}
_changes: list[dict[str, Any]] = []


def _add(item_id: str, name: str, parent: str, mime: str, content: bytes | None = None) -> None:
    resource: dict[str, Any] = {
        "id": item_id,
        "name": name,
        "mimeType": mime,
        "parents": [parent],
        "trashed": False,
        "modifiedTime": "2026-11-26T10:00:00.000Z",
    }
    if content is not None:
        _contents[item_id] = content
        resource["size"] = str(len(content))
        resource["md5Checksum"] = hashlib.md5(content).hexdigest()  # noqa: S324 - as Drive does
    _files[item_id] = resource


_add("folder-e2e-sales", "E2E Sales", "root", FOLDER)
_add("file-pricing", "pricing.md", "folder-e2e-sales", "text/markdown", PRICING)
_add("file-notes", "notes.txt", "folder-e2e-sales", "text/plain", NOTES)
_add("file-outside", "outside.txt", "root", "text/plain", OUTSIDE)


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def _tokens(*, with_refresh: bool) -> dict[str, Any]:
    access = "e2e-access-" + secrets.token_urlsafe(16)
    body: dict[str, Any] = {
        "access_token": access,
        "expires_in": 3599,
        "scope": SCOPE,
        "token_type": "Bearer",
    }
    _access.add(access)
    _issued.append(access)
    if with_refresh:
        refresh = "e2e-refresh-" + secrets.token_urlsafe(24)
        _refresh.add(refresh)
        _issued.append(refresh)
        body["refresh_token"] = refresh
    return body


def _public(resource: dict[str, Any]) -> dict[str, Any]:
    return dict(resource)


class Handler(BaseHTTPRequestHandler):
    server_version = "fake-google"

    def log_message(self, format: str, *args: Any) -> None:
        return  # quiet: request lines would carry codes and tokens

    def _send(self, status: int, body: bytes = b"", headers: dict[str, str] | None = None) -> None:
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: Any) -> None:
        self._send(status, json.dumps(payload).encode(), {"Content-Type": "application/json"})

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(min(length, MAX_BODY))

    def _form(self) -> dict[str, str]:
        return {key: values[0] for key, values in parse_qs(self._body().decode()).items()}

    # --- OAuth --------------------------------------------------------------------------------------------

    def _authorize(self, query: dict[str, str]) -> None:
        valid = (
            query.get("client_id") == CLIENT_ID
            and query.get("redirect_uri") == REDIRECT_URI
            and query.get("response_type") == "code"
            and query.get("code_challenge_method") == "S256"
            and query.get("access_type") == "offline"
            and SCOPE in query.get("scope", "").split()
            and query.get("state")
            and query.get("code_challenge")
        )
        if not valid:
            self._send(400, b"invalid authorization request", {"Content-Type": "text/plain"})
            return
        code = "e2e-code-" + secrets.token_urlsafe(16)
        with _lock:
            _codes[code] = (query["code_challenge"], query["redirect_uri"])
        location = (
            query["redirect_uri"] + "?" + urlencode({"state": query["state"], "code": code, "scope": SCOPE})
        )
        self._send(302, headers={"Location": location, "Cache-Control": "no-store"})

    def _token(self) -> None:
        form = self._form()
        if (
            form.get("client_id") != CLIENT_ID
            or not CLIENT_SECRET
            or form.get("client_secret") != CLIENT_SECRET
        ):
            self._json(401, {"error": "invalid_client"})
            return
        with _lock:
            if form.get("grant_type") == "authorization_code":
                issued = _codes.pop(form.get("code", ""), None)
                if (
                    issued is None
                    or issued[1] != form.get("redirect_uri")
                    or _challenge(form.get("code_verifier", "")) != issued[0]
                ):
                    self._json(400, {"error": "invalid_grant"})
                    return
                self._json(200, _tokens(with_refresh=True))
                return
            if form.get("grant_type") == "refresh_token":
                if form.get("refresh_token") not in _refresh:
                    self._json(400, {"error": "invalid_grant"})
                    return
                self._json(200, _tokens(with_refresh=False))
                return
        self._json(400, {"error": "unsupported_grant_type"})

    def _revoke(self) -> None:
        token = self._form().get("token", "")
        with _lock:
            known = token in _refresh or token in _access
            _refresh.discard(token)
            _access.discard(token)
        if known:
            self._send(200)
        else:
            self._json(400, {"error": "invalid_token"})

    # --- Drive --------------------------------------------------------------------------------------------

    def _list(self, query: dict[str, str]) -> None:
        q = query.get("q", "")
        folders_only = f"mimeType = '{FOLDER}'" in q
        match = PARENT.search(q)
        parent = match.group(1).replace("\\'", "'") if match is not None else None
        if parent is not None and parent != "root" and parent not in _files:
            self._json(404, {"error": {"code": 404, "errors": [{"reason": "notFound"}]}})
            return
        found = []
        for resource in _files.values():
            if resource["trashed"] or (folders_only and resource["mimeType"] != FOLDER):
                continue
            if "sharedWithMe = true" in q:
                if "root" not in resource["parents"]:
                    continue
            elif parent is None or parent not in resource["parents"]:
                continue
            found.append(_public(resource))
        found.sort(key=lambda item: item["name"])
        start, size = int(query.get("pageToken", "0")), int(query.get("pageSize", "100"))
        body: dict[str, Any] = {"files": found[start : start + size]}
        if start + size < len(found):
            body["nextPageToken"] = str(start + size)
        self._json(200, body)

    def _change_page(self, query: dict[str, str]) -> None:
        token = query.get("pageToken", "")
        if not token.isdigit() or int(token) > len(_changes):
            self._json(400, {"error": {"code": 400, "message": "Invalid Value"}})
            return
        start, size = int(token), int(query.get("pageSize", "100"))
        page = []
        for entry in _changes[start : start + size]:
            change: dict[str, Any] = {
                "changeType": "file",
                "fileId": entry["fileId"],
                "removed": entry["removed"],
            }
            resource = _files.get(entry["fileId"])
            if not entry["removed"] and resource is not None:
                change["file"] = _public(resource)
            page.append(change)
        body: dict[str, Any] = {"changes": page}
        if start + size < len(_changes):
            body["nextPageToken"] = str(start + size)
        else:
            body["newStartPageToken"] = str(len(_changes))
        self._json(200, body)

    def _drive(self, path: str, query: dict[str, str]) -> None:
        header = self.headers.get("Authorization", "")
        with _lock:
            authorized = header.startswith("Bearer ") and header[7:] in _access
        if not authorized:
            self._json(401, {"error": {"code": 401}})
            return
        with _lock:
            if path == "about":
                self._json(
                    200, {"user": {"displayName": "E2E Drive Bot", "emailAddress": "e2e-drive@example.com"}}
                )
            elif path == "drives":
                self._json(200, {"drives": []})
            elif path == "files":
                self._list(query)
            elif path == "changes/startPageToken":
                self._json(200, {"startPageToken": str(len(_changes))})
            elif path == "changes":
                self._change_page(query)
            elif path.startswith("files/") and path.count("/") == 1:
                item_id = unquote(path.removeprefix("files/"))
                resource = _files.get(item_id)
                if resource is None:
                    self._json(404, {"error": {"code": 404}})
                elif query.get("alt") == "media":
                    self._send(200, _contents.get(item_id, b""), {"Content-Type": resource["mimeType"]})
                else:
                    self._json(200, _public(resource))
            else:
                self._json(404, {"error": {"code": 404}})

    # --- test control -------------------------------------------------------------------------------------

    def _remove(self) -> None:
        try:
            name = str(json.loads(self._body() or b"{}").get("name", ""))
        except ValueError:
            name = ""
        with _lock:
            item_id = next((key for key, value in _files.items() if value["name"] == name), None)
            if item_id is None:
                self._json(404, {"error": "unknown file"})
                return
            del _files[item_id]
            _contents.pop(item_id, None)
            _changes.append({"fileId": item_id, "removed": True})
        self._json(200, {"removed": quote(item_id)})

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        query = {key: values[0] for key, values in parse_qs(parts.query).items()}
        if parts.path == "/health":
            self._json(200, {"status": "ok"})
        elif parts.path == "/o/oauth2/v2/auth":
            self._authorize(query)
        elif parts.path.startswith("/drive/v3/"):
            self._drive(parts.path.removeprefix("/drive/v3/"), query)
        elif parts.path == "/_control/tokens":
            with _lock:
                self._json(200, {"tokens": list(_issued)})
        else:
            self._json(404, {"error": {"code": 404}})

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path == "/token":
            self._token()
        elif path == "/revoke":
            self._revoke()
        elif path == "/_control/remove":
            self._remove()
        else:
            self._json(404, {"error": {"code": 404}})


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", 8080), Handler)  # noqa: S104 - inside the e2e network only
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
