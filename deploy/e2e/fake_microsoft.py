"""Fake Microsoft identity platform and Graph v1.0 for the end-to-end tests (compose.e2e.yaml, ADR-021). Test
infrastructure only.

Standard library only, no network access. The backend reaches it through ``[connectors.microsoft_365]`` and
the allowlist of ``deploy/e2e/plummy.toml``; the browser opens its sign-in page, which approves at once:

- ``GET /{tenant}/oauth2/v2.0/authorize`` checks the request (client ID, exact redirect URI, ``code`` + S256
  PKCE, ``offline_access`` and the Graph permissions) and answers ``302`` to the redirect URI with a one-time
  code and the ``state``;
- ``POST /{tenant}/oauth2/v2.0/token`` exchanges a code (client secret, redirect URI and PKCE verifier
  checked) or a refresh token, which it rotates; there is no revoke endpoint, as at Microsoft;
- ``GET /v1.0/...`` serves ``me``, ``sites``, a site's ``drives``, items, ``children``, ``content`` and
  ``root/delta`` for a fixed tree: the site ``E2E Legal`` with the library ``Documents``, its folder
  ``Contracts`` (``nda.md``, ``terms.txt``) and ``outside.txt`` in the library's root;
- ``POST /_control/remove`` with ``{"name": …}`` deletes a file as if in SharePoint (a ``deleted`` item in the
  delta); ``GET /_control/tokens`` lists every token it issued, so a test can prove none reached the browser.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

TENANT = os.environ.get("FAKE_MICROSOFT_TENANT", "e2e.onmicrosoft.com")
CLIENT_ID = os.environ.get("FAKE_MICROSOFT_CLIENT_ID", "00000000-0000-4000-8000-0000000e2e01")
CLIENT_SECRET = os.environ.get("FAKE_MICROSOFT_CLIENT_SECRET", "")
REDIRECT_URI = os.environ.get(
    "FAKE_MICROSOFT_REDIRECT_URI", "http://localhost:5173/api/v1/data-sources/oauth/callback"
)
GRAPH = "https://graph.microsoft.com/"
GRANTED = f"{GRAPH}Files.Read.All {GRAPH}Sites.Read.All {GRAPH}User.Read"
SITE = "e2e.sharepoint.com,7f3c0d1e-0000-4000-8000-000000000001,7f3c0d1e-0000-4000-8000-000000000002"
DRIVE = "b!e2eLegalDocumentsLibrary"
ROOT = "01E2EROOT"
MAX_BODY = 64 * 1024

NDA = b"""# Mutual NDA

The confidentiality code of the NDA is Velmora-2281. It applies for three years.
"""
TERMS = b"Terms: invoices are due within 30 days.\n"
OUTSIDE = b"Outside the synced folder: Orrinvale-5530.\n"

_lock = threading.Lock()
_codes: dict[str, tuple[str, str]] = {}  # code -> (PKCE challenge, redirect URI)
_refresh: set[str] = set()
_access: set[str] = set()
_issued: list[str] = []
_items: dict[str, dict[str, Any]] = {}
_contents: dict[str, bytes] = {}
_log: list[str] = []  # the delta log of the library: a token is an index into it


def _put(item_id: str, name: str, parent: str | None, content: bytes | None = None) -> None:
    resource: dict[str, Any] = {
        "id": item_id,
        "name": name,
        "lastModifiedDateTime": "2026-11-26T10:00:00Z",
        "cTag": f'"c:{{{item_id}}},1"',
        "parentReference": {"driveId": DRIVE, **({"id": parent} if parent else {})},
    }
    if parent is None:
        resource["root"] = {}
    if content is None:
        resource["folder"] = {"childCount": 0}
    else:
        _contents[item_id] = content
        resource["size"] = len(content)
        digest = base64.b64encode(hashlib.sha1(content).digest()).decode()  # noqa: S324 - a fake hash
        resource["file"] = {"mimeType": "text/plain", "hashes": {"quickXorHash": digest}}
    _items[item_id] = resource
    _log.append(item_id)


_put(ROOT, "root", None)
_put("01E2ECONTRACTS", "Contracts", ROOT)
_put("01E2ENDA", "nda.md", "01E2ECONTRACTS", NDA)
_put("01E2ETERMS", "terms.txt", "01E2ECONTRACTS", TERMS)
_put("01E2EOUTSIDE", "outside.txt", ROOT, OUTSIDE)


def _challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()


def _tokens() -> dict[str, Any]:
    access = "eyJ0eXAi.e2e-access-" + secrets.token_urlsafe(16)
    refresh = "1.AQ-e2e-refresh-" + secrets.token_urlsafe(24)
    _access.add(access)
    _refresh.add(refresh)
    _issued.extend([access, refresh])
    return {
        "token_type": "Bearer",
        "scope": GRANTED,
        "expires_in": 3599,
        "access_token": access,
        "refresh_token": refresh,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "fake-microsoft"

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

    def _missing(self) -> None:
        self._json(404, {"error": {"code": "itemNotFound"}})

    def _body(self) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(min(length, MAX_BODY))

    def _link(self, path: str, query: dict[str, str]) -> str:
        return f"http://{self.headers.get('Host', 'fake-microsoft:8080')}/v1.0/{path}?{urlencode(query)}"

    # --- identity platform --------------------------------------------------------------------------------

    def _authorize(self, query: dict[str, str]) -> None:
        scopes = query.get("scope", "").split()
        valid = (
            query.get("client_id") == CLIENT_ID
            and query.get("redirect_uri") == REDIRECT_URI
            and query.get("response_type") == "code"
            and query.get("code_challenge_method") == "S256"
            and "offline_access" in scopes
            and f"{GRAPH}Files.Read.All" in scopes
            and query.get("state")
            and query.get("code_challenge")
        )
        if not valid:
            self._send(400, b"invalid authorization request", {"Content-Type": "text/plain"})
            return
        code = "e2e-code-" + secrets.token_urlsafe(16)
        with _lock:
            _codes[code] = (query["code_challenge"], query["redirect_uri"])
        location = query["redirect_uri"] + "?" + urlencode({"code": code, "state": query["state"]})
        self._send(302, headers={"Location": location, "Cache-Control": "no-store"})

    def _token(self) -> None:
        form = {key: values[0] for key, values in parse_qs(self._body().decode()).items()}
        client_ok = form.get("client_id") == CLIENT_ID and CLIENT_SECRET != ""
        if not client_ok or form.get("client_secret") != CLIENT_SECRET:
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
                self._json(200, _tokens())
                return
            if form.get("grant_type") == "refresh_token":
                token = form.get("refresh_token", "")
                if token not in _refresh:
                    self._json(400, {"error": "invalid_grant"})
                    return
                _refresh.discard(token)  # rotated
                self._json(200, _tokens())
                return
        self._json(400, {"error": "unsupported_grant_type"})

    # --- Graph --------------------------------------------------------------------------------------------

    @staticmethod
    def _public(resource: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in resource.items() if not key.startswith("_")}

    def _children(self, item_id: str, query: dict[str, str]) -> None:
        if item_id not in _items or "deleted" in _items[item_id]:
            self._missing()
            return
        children = sorted(
            (
                self._public(resource)
                for resource in _items.values()
                if "deleted" not in resource and resource.get("parentReference", {}).get("id") == item_id
            ),
            key=lambda resource: str(resource["name"]),
        )
        start, size = int(query.get("$skiptoken", "0")), int(query.get("$top", "200"))
        body: dict[str, Any] = {"value": children[start : start + size]}
        if start + size < len(children):
            path = f"drives/{quote(DRIVE, safe='')}/items/{quote(item_id, safe='')}/children"
            body["@odata.nextLink"] = self._link(path, {"$skiptoken": str(start + size)})
        self._json(200, body)

    def _delta(self, query: dict[str, str]) -> None:
        path = f"drives/{quote(DRIVE, safe='')}/root/delta"
        token = query.get("token", "")
        if token == "latest":  # noqa: S105 - a Graph delta keyword, not a secret
            self._json(200, {"value": [], "@odata.deltaLink": self._link(path, {"token": str(len(_log))})})
            return
        if not token.isdigit() or int(token) > len(_log):
            self._json(410, {"error": {"code": "resyncRequired"}})
            return
        start = int(token)
        seen = [self._public(_items[item_id]) for item_id in _log[start:]]
        self._json(200, {"value": seen, "@odata.deltaLink": self._link(path, {"token": str(len(_log))})})

    def _item(self, item_id: str, rest: list[str], query: dict[str, str]) -> None:
        resource = _items.get(item_id)
        if resource is None or "deleted" in resource:
            self._missing()
        elif rest == ["content"]:
            self._send(200, _contents.get(item_id, b""), {"Content-Type": "application/octet-stream"})
        elif rest == ["children"]:
            self._children(item_id, query)
        elif rest == []:
            self._json(200, self._public(resource))
        else:
            self._missing()

    def _graph(self, path: str, query: dict[str, str]) -> None:
        header = self.headers.get("Authorization", "")
        with _lock:
            authorized = header.startswith("Bearer ") and header[7:] in _access
        if not authorized:
            self._json(401, {"error": {"code": "InvalidAuthenticationToken"}})
            return
        parts = [unquote(part) for part in path.split("/")]
        with _lock:
            if parts == ["me"]:
                me = {"displayName": "E2E Graph Bot", "userPrincipalName": "e2e-graph@e2e.example"}
                self._json(200, me)
            elif parts == ["me", "drive"]:
                self._missing()  # the service account has no OneDrive licence: only sites
            elif parts == ["sites"]:
                self._json(200, {"value": [{"id": SITE, "displayName": "E2E Legal", "name": "e2e-legal"}]})
            elif parts == ["sites", SITE]:
                self._json(200, {"id": SITE, "displayName": "E2E Legal", "name": "e2e-legal"})
            elif parts == ["sites", SITE, "drives"]:
                library = {"id": DRIVE, "name": "Documents", "driveType": "documentLibrary"}
                self._json(200, {"value": [library]})
            elif parts[:2] == ["drives", DRIVE]:
                rest = parts[2:]
                if rest == []:
                    self._json(200, {"id": DRIVE, "name": "Documents", "driveType": "documentLibrary"})
                elif rest[:1] == ["root"]:
                    if rest == ["root", "delta"]:
                        self._delta(query)
                    else:
                        self._item(ROOT, rest[1:], query)
                elif rest[:1] == ["items"] and len(rest) >= 2:
                    self._item(rest[1], rest[2:], query)
                else:
                    self._missing()
            else:
                self._missing()

    # --- test control -------------------------------------------------------------------------------------

    def _remove(self) -> None:
        try:
            name = str(json.loads(self._body() or b"{}").get("name", ""))
        except ValueError:
            name = ""
        with _lock:
            item_id = next(
                (key for key, item in _items.items() if item.get("name") == name and "deleted" not in item),
                None,
            )
            if item_id is None:
                self._json(404, {"error": "unknown file"})
                return
            _items[item_id] = {"id": item_id, "deleted": {"state": "deleted"}}
            _contents.pop(item_id, None)
            _log.append(item_id)
        self._json(200, {"removed": item_id})

    def do_GET(self) -> None:
        parts = urlsplit(self.path)
        query = {key: values[0] for key, values in parse_qs(parts.query).items()}
        if parts.path == "/health":
            self._json(200, {"status": "ok"})
        elif parts.path == f"/{TENANT}/oauth2/v2.0/authorize":
            self._authorize(query)
        elif parts.path.startswith("/v1.0/"):
            self._graph(parts.path.removeprefix("/v1.0/"), query)
        elif parts.path == "/_control/tokens":
            with _lock:
                self._json(200, {"tokens": list(_issued)})
        else:
            self._missing()

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        if path == f"/{TENANT}/oauth2/v2.0/token":
            self._token()
        elif path == "/_control/remove":
            self._remove()
        else:
            self._missing()


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", 8080), Handler)  # noqa: S104 - inside the e2e network only
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
