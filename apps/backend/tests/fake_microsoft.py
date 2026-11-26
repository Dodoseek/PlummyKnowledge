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
import itertools
import secrets
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, quote, unquote, urlencode, urlsplit

from tests.fake_http import ReceivedRequest, Reply

TENANT = "contoso.onmicrosoft.com"
CLIENT_ID = "00000000-0000-4000-8000-00000000c11e"
CLIENT_SECRET = "test-entra-client-secret"
GRAPH = "https://graph.microsoft.com/"
# What Entra answers in ``scope``: Graph permissions with the resource prefix, offline_access without it.
GRANTED = f"{GRAPH}Files.Read.All {GRAPH}Sites.Read.All {GRAPH}User.Read"
_ids = itertools.count(1)


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
    # Graph: drives by id ("_root" is the root item, "_site" the site), items by (drive, item), contents,
    # the change log of each drive (a delta token is an index into it), sites
    drives: dict[str, dict[str, Any]] = field(default_factory=dict)
    items: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    contents: dict[tuple[str, str], bytes] = field(default_factory=dict)
    log: dict[str, list[str]] = field(default_factory=dict)
    sites: list[dict[str, str]] = field(default_factory=list)
    own_drive: str | None = None
    page_size: int | None = None  # overrides $top of children and delta pages
    download_redirect: str | None = None  # …/content answers 302 to this base URL
    link_base: str | None = None  # base URL of nextLink/deltaLink (default: the request's own host)

    # --- Graph tree ---------------------------------------------------------------------------------------

    def drive(self, name: str, *, site: str | None = None, own: bool = False) -> str:
        drive_id = "b!" + secrets.token_urlsafe(12)
        root_id = f"01ROOT{next(_ids):04d}"
        self.drives[drive_id] = {
            "id": drive_id,
            "name": name,
            "driveType": "business" if own else "documentLibrary",
            "_root": root_id,
            "_site": site,
        }
        self.items[(drive_id, root_id)] = {
            "id": root_id,
            "name": "root",
            "root": {},
            "folder": {"childCount": 0},
            "parentReference": {"driveId": drive_id, "driveType": "documentLibrary"},
            "lastModifiedDateTime": "2026-11-26T10:00:00Z",
        }
        self.log[drive_id] = []
        if own:
            self.own_drive = drive_id
        return drive_id

    def site(self, name: str) -> str:
        site_id = f"contoso.sharepoint.com,{secrets.token_hex(4)}-0000-4000-8000-000000000001,{next(_ids)}"
        self.sites.append({"id": site_id, "displayName": name, "name": name.lower()})
        return site_id

    def root(self, drive_id: str) -> str:
        return str(self.drives[drive_id]["_root"])

    def add(
        self,
        drive_id: str,
        name: str,
        *,
        parent: str | None = None,
        content: bytes | None = None,
        folder: bool = False,
        **facets: Any,
    ) -> str:
        item_id = f"01ITEM{next(_ids):04d}{secrets.token_hex(3).upper()}"
        resource: dict[str, Any] = {
            "id": item_id,
            "name": name,
            "parentReference": {"driveId": drive_id, "id": parent or self.root(drive_id)},
            "lastModifiedDateTime": "2026-11-26T10:00:00Z",
            "cTag": f'"c:{{{item_id}}},1"',
            **facets,
        }
        if folder:
            resource["folder"] = {"childCount": 0}
        elif content is not None:
            self.contents[(drive_id, item_id)] = content
            resource["size"] = len(content)
            resource["file"] = {
                "mimeType": "text/plain",
                "hashes": {"quickXorHash": base64.b64encode(hashlib.sha1(content).digest()).decode()},  # noqa: S324
            }
        self.items[(drive_id, item_id)] = resource
        self.log[drive_id].append(item_id)
        return item_id

    def folder(self, drive_id: str, name: str, *, parent: str | None = None) -> str:
        return self.add(drive_id, name, parent=parent, folder=True)

    def change(self, drive_id: str, item_id: str, **updates: Any) -> None:
        self.items[(drive_id, item_id)].update(updates)
        self.log[drive_id].append(item_id)

    def set_content(self, drive_id: str, item_id: str, content: bytes) -> None:
        """New content: a new hash and size, and a change in the log."""
        resource = self.items[(drive_id, item_id)]
        self.contents[(drive_id, item_id)] = content
        resource["size"] = len(content)
        resource["file"]["hashes"]["quickXorHash"] = base64.b64encode(hashlib.sha1(content).digest()).decode()  # noqa: S324
        self.log[drive_id].append(item_id)

    def delete(self, drive_id: str, item_id: str) -> None:
        """Deletes an item and everything under it; only the item itself is in the log (the worst case of
        Graph, which need not report each child of a deleted folder)."""
        doomed = [item_id]
        while doomed:
            current = doomed.pop()
            resource = self.items[(drive_id, current)]
            self.items[(drive_id, current)] = {
                "id": current,
                "deleted": {"state": "deleted"},
                "_was": resource,
            }
            self.contents.pop((drive_id, current), None)
            doomed.extend(
                child
                for (drive, child), value in self.items.items()
                if drive == drive_id and value.get("parentReference", {}).get("id") == current
            )
        self.log[drive_id].append(item_id)

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

    @staticmethod
    def _public(resource: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in resource.items() if not key.startswith("_")}

    def _link(self, request: ReceivedRequest, path: str, query: dict[str, str]) -> str:
        base = self.link_base or f"http://{request.headers['host']}"
        return f"{base}/v1.0/{path}?{urlencode(query)}"

    def _children(
        self, request: ReceivedRequest, drive_id: str, item_id: str, query: dict[str, str]
    ) -> Reply:
        if (drive_id, item_id) not in self.items or "deleted" in self.items[(drive_id, item_id)]:
            return Reply.json({"error": {"code": "itemNotFound"}}, status=404)
        children = sorted(
            (
                self._public(resource)
                for (drive, _), resource in self.items.items()
                if drive == drive_id
                and "deleted" not in resource
                and resource.get("parentReference", {}).get("id") == item_id
            ),
            key=lambda resource: str(resource["name"]),
        )
        start = int(query.get("$skiptoken", "0"))
        size = self.page_size or int(query.get("$top", "200"))
        body: dict[str, Any] = {"value": children[start : start + size]}
        if start + size < len(children):
            path = f"drives/{quote(drive_id, safe='')}/items/{quote(item_id, safe='')}/children"
            body["@odata.nextLink"] = self._link(request, path, {"$skiptoken": str(start + size)})
        return Reply.json(body)

    def _delta(self, request: ReceivedRequest, drive_id: str, query: dict[str, str]) -> Reply:
        log = self.log[drive_id]
        path = f"drives/{quote(drive_id, safe='')}/root/delta"
        token = query.get("token", "")
        if token == "latest":
            return Reply.json(
                {"value": [], "@odata.deltaLink": self._link(request, path, {"token": str(len(log))})}
            )
        if not token.isdigit() or int(token) > len(log):
            return Reply.json({"error": {"code": "resyncRequired"}}, status=410)
        start, size = int(token), self.page_size or 200
        seen: list[dict[str, Any]] = []
        for item_id in log[start : start + size]:
            resource = self._public(self.items[(drive_id, item_id)])
            seen.append(resource)
        body: dict[str, Any] = {"value": seen}
        if start + size < len(log):
            body["@odata.nextLink"] = self._link(request, path, {"token": str(start + size)})
        else:
            body["@odata.deltaLink"] = self._link(request, path, {"token": str(len(log))})
        return Reply.json(body)

    def _item(self, drive_id: str, item_id: str, rest: list[str]) -> Reply:
        resource = self.items.get((drive_id, item_id))
        if resource is None or "deleted" in resource:
            return Reply.json({"error": {"code": "itemNotFound"}}, status=404)
        if rest == ["content"]:
            if self.download_redirect is not None:
                return Reply(302, {"Location": f"{self.download_redirect}/download/{item_id}?tempauth=x"})
            return Reply(
                200, {"Content-Type": "application/octet-stream"}, self.contents.get((drive_id, item_id), b"")
            )
        return Reply.json(self._public(resource))

    def _graph(self, request: ReceivedRequest, path: str, query: dict[str, str]) -> Reply:
        if not self._bearer(request):
            return Reply.json({"error": {"code": "InvalidAuthenticationToken"}}, status=401)
        if self.api_failures:
            return self.api_failures.pop(0)
        parts = [unquote(part) for part in path.split("/")]
        if parts == ["me"]:
            return Reply.json(self.user)
        if parts == ["me", "drive"]:
            if self.own_drive is None:
                return Reply.json({"error": {"code": "itemNotFound"}}, status=404)
            return Reply.json(self._public(self.drives[self.own_drive]))
        if parts == ["sites"]:
            return Reply.json({"value": self.sites})
        if len(parts) == 2 and parts[0] == "sites":
            site = next((site for site in self.sites if site["id"] == parts[1]), None)
            return Reply.json(site) if site else Reply.json({"error": {"code": "itemNotFound"}}, status=404)
        if len(parts) == 3 and parts[0] == "sites" and parts[2] == "drives":
            if not any(site["id"] == parts[1] for site in self.sites):
                return Reply.json({"error": {"code": "itemNotFound"}}, status=404)
            drives = [self._public(drive) for drive in self.drives.values() if drive["_site"] == parts[1]]
            return Reply.json({"value": drives})
        if len(parts) >= 2 and parts[0] == "drives":
            drive_id = parts[1]
            if drive_id not in self.drives:
                return Reply.json({"error": {"code": "itemNotFound"}}, status=404)
            rest = parts[2:]
            if rest == []:
                return Reply.json(self._public(self.drives[drive_id]))
            if rest == ["root"]:
                return self._item(drive_id, self.root(drive_id), [])
            if rest == ["root", "children"]:
                return self._children(request, drive_id, self.root(drive_id), query)
            if rest == ["root", "delta"]:
                return self._delta(request, drive_id, query)
            if len(rest) >= 2 and rest[0] == "items":
                if rest[2:] == ["children"]:
                    return self._children(request, drive_id, rest[1], query)
                return self._item(drive_id, rest[1], rest[2:])
        return Reply.json({"error": {"code": "itemNotFound"}}, status=404)

    def handle(self, request: ReceivedRequest) -> Reply:
        parts = urlsplit(request.target)
        path = parts.path
        if request.method == "POST" and path == f"/{TENANT}/oauth2/v2.0/token":
            return self._token(request)
        if request.method == "GET" and path.startswith("/v1.0/"):
            query = {key: values[0] for key, values in parse_qs(parts.query).items()}
            return self._graph(request, path.removeprefix("/v1.0/"), query)
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
