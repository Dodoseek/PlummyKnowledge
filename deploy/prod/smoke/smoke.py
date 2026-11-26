"""Smoke test of the production stack through Nginx (ADR-023; CI job "Production stack").

Standard library only. Runs on the host against the published ports:

    python3 deploy/prod/smoke/smoke.py --base-url https://localhost:8443 --http-url http://localhost:8080 \
        --ca-file <cert.pem> --setup-token-file <file>

Checks the HTTP → HTTPS redirect, the security headers and CSP, readiness, that Swagger UI is off, then the
core scenario as an administrator would run it: setup → provider (the fake OpenAI-compatible server) →
models → embedding profile → knowledge base → upload → indexed → search → assistant → streamed answer with a
verified citation → logout. Prints step names only: no tokens, passwords or document text.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import secrets
import ssl
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any

CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
    "font-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; "
    "form-action 'self'"
)
HEADERS = {
    "content-security-policy": CSP,
    "strict-transport-security": "max-age=31536000",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
    "permissions-policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
    "cross-origin-opener-policy": "same-origin",
    "cross-origin-resource-policy": "same-origin",
    "x-frame-options": "DENY",
}
DOCUMENT = """# Travel policy

## Hotels

Business trips are booked through the travel desk. The hotel limit is 150 euros per night.
"""


class Failure(Exception):
    pass


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class Client:
    def __init__(self, base_url: str, context: ssl.SSLContext) -> None:
        self.base_url = base_url
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=context),
            urllib.request.HTTPCookieProcessor(self.jar),
            NoRedirect(),
        )
        self.csrf: str | None = None

    def request(
        self,
        method: str,
        path: str,
        body: Any = None,
        *,
        raw: bytes | None = None,
        content_type: str | None = None,
        timeout: float = 30,
    ) -> tuple[int, dict[str, str], bytes]:
        headers = {"Origin": self.base_url, "Accept": "application/json, text/event-stream"}
        data = raw
        if body is not None:
            data = json.dumps(body).encode()
            content_type = "application/json"
        if content_type:
            headers["Content-Type"] = content_type
        if self.csrf and method != "GET":
            headers["X-CSRF-Token"] = self.csrf
        url = self.base_url + path  # https only (main)
        req = urllib.request.Request(url, data=data, method=method, headers=headers)  # noqa: S310
        try:
            with self.opener.open(req, timeout=timeout) as response:
                return response.status, {k.lower(): v for k, v in response.headers.items()}, response.read()
        except urllib.error.HTTPError as error:
            return error.code, {k.lower(): v for k, v in error.headers.items()}, error.read()

    def json(self, method: str, path: str, body: Any = None, *, expect: int = 200) -> Any:
        status, _, content = self.request(method, "/api/v1" + path, body)
        if status != expect:
            # Problem details carry a code and a safe message only.
            raise Failure(f"{method} {path}: {status} {content[:300]!r}")
        return json.loads(content) if content else None


def step(name: str) -> None:
    print(f"- {name}", flush=True)


def check_headers(headers: dict[str, str], where: str) -> None:
    for name, value in HEADERS.items():
        if headers.get(name) != value:
            raise Failure(f"{where}: header {name} is {headers.get(name)!r}")
    if headers.get("server", "") != "nginx":
        raise Failure(f"{where}: the Server header discloses a version: {headers.get('server')!r}")


def multipart(fields: dict[str, str], filename: str, content: bytes) -> tuple[bytes, str]:
    boundary = "smoke" + secrets.token_hex(12)
    parts = [
        f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode()
        for name, value in fields.items()
    ]
    parts.append(
        f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: text/markdown\r\n\r\n".encode()
        + content
        + f"\r\n--{boundary}--\r\n".encode()
    )
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def sse_events(content: bytes) -> list[tuple[str, dict[str, Any]]]:
    events = []
    for block in content.decode().split("\n\n"):
        name, data = None, None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if name is not None and data is not None:
            events.append((name, data))
    return events


def run(args: argparse.Namespace) -> None:
    context = ssl.create_default_context(cafile=args.ca_file)
    client = Client(args.base_url.rstrip("/"), context)

    step("HTTP redirects to HTTPS")
    plain = urllib.request.build_opener(NoRedirect())
    try:
        plain.open(args.http_url.rstrip("/") + "/knowledge-bases?x=1", timeout=10)  # noqa: S310
        raise Failure("plain HTTP did not redirect")
    except urllib.error.HTTPError as error:
        location = error.headers.get("Location", "")
        if error.code != 301 or not location.startswith("https://") or not location.endswith("?x=1"):
            raise Failure(f"redirect: {error.code} {location!r}") from None

    step("SPA shell, security headers and CSP")
    status, headers, content = client.request("GET", "/admin/users")
    if status != 200 or b'<div id="root">' not in content:
        raise Failure(f"SPA fallback: {status}")
    check_headers(headers, "SPA")
    if headers.get("cache-control") != "no-cache":
        raise Failure("the application shell must not be cached")
    if b"<script>" in content or b"<style>" in content:
        raise Failure("inline script or style in index.html (CSP)")

    step("API readiness and headers")
    deadline = time.monotonic() + 120
    while True:
        status, headers, _ = client.request("GET", "/api/v1/health/ready")
        if status == 200:
            break
        if time.monotonic() > deadline:
            raise Failure(f"readiness: {status}")
        time.sleep(2)
    check_headers(headers, "API")
    status, _, _ = client.request("GET", "/api/v1/docs")
    if status != 404:
        raise Failure(f"Swagger UI is reachable in production: {status}")

    step("first administrator through the setup token")
    token = open(args.setup_token_file, encoding="utf-8").read().strip()
    if client.json("GET", "/setup/status")["status"] != "uninitialized":
        raise Failure("the instance is already initialized (use a fresh stack)")
    password = "Smoke-" + secrets.token_urlsafe(18)
    session = client.json(
        "POST",
        "/setup/initialize",
        {
            "bootstrap_token": token,
            "admin": {
                "email": "smoke-admin@example.com",
                "display_name": "Smoke Admin",
                "password": password,
            },
        },
        expect=201,
    )
    client.csrf = session["csrf_token"]
    cookies = [cookie for cookie in client.jar if cookie.value]
    if not cookies or not all(cookie.secure for cookie in cookies):
        raise Failure("the session cookie is not Secure")

    step("provider, models, embedding profile")
    provider = client.json(
        "POST",
        "/providers",
        {
            "name": "Smoke fake LLM",
            "provider_type": "openai_compatible",
            "base_url": "http://fake-llm:8080/v1",
            "settings": {"timeout_seconds": 30},
            "enabled": True,
        },
        expect=201,
    )
    if client.json("POST", f"/providers/{provider['id']}/test")["status"] != "ok":
        raise Failure("provider connection test failed")
    model = client.json(
        "POST",
        f"/providers/{provider['id']}/models",
        {"model_id": "fake-chat", "context_window": 32000, "max_output_tokens": 1024},
        expect=201,
    )
    profile = client.json(
        "POST",
        "/embedding-profiles",
        {
            "name": "Smoke embeddings",
            "provider_id": provider["id"],
            "model_id": "fake-embed",
            "dimensions": 64,
            "distance_metric": "cosine",
            "normalize": True,
        },
        expect=201,
    )

    step("knowledge base, upload to S3 storage, indexing")
    kb = client.json(
        "POST", "/knowledge-bases", {"name": "Smoke KB", "embedding_profile_id": profile["id"]}, expect=201
    )
    body, content_type = multipart({"knowledge_base_id": kb["id"]}, "travel.md", DOCUMENT.encode())
    status, _, content = client.request("POST", "/api/v1/documents", raw=body, content_type=content_type)
    if status not in (201, 202):
        raise Failure(f"upload: {status} {content[:300]!r}")
    document_id = json.loads(content)["document"]["id"]
    deadline = time.monotonic() + 240
    while True:
        state = client.json("GET", f"/documents/{document_id}")["processing_status"]
        if state == "indexed":
            break
        if state == "failed" or time.monotonic() > deadline:
            raise Failure(f"document is {state}")
        time.sleep(2)

    step("search")
    found = client.json(
        "POST", f"/knowledge-bases/{kb['id']}/search", {"query": "hotel limit", "mode": "hybrid"}
    )
    if not found["items"]:
        raise Failure("the document was not found")

    step("assistant and streamed answer with a citation")
    assistant = client.json(
        "POST",
        "/assistants",
        {
            "name": "Smoke assistant",
            "instructions": "Answer from the documents only.",
            "model_config_id": model["id"],
            "embedding_profile_id": profile["id"],
            "knowledge_base_ids": [kb["id"]],
        },
        expect=201,
    )
    status, headers, content = client.request(
        "POST",
        "/api/v1/chat/stream",
        {
            "assistant_id": assistant["id"],
            "content": "What is the hotel limit?",
            "client_message_id": str(uuid.uuid4()),
        },
        timeout=180,
    )
    if status != 200 or not headers.get("content-type", "").startswith("text/event-stream"):
        raise Failure(f"chat stream: {status}")
    events = sse_events(content)
    names = [name for name, _ in events]
    if "token" not in names or "citation" not in names or names[-1] != "message_complete":
        raise Failure(f"chat stream events: {sorted(set(names))}")
    issued = {data["citation_id"] for name, data in events if name == "citation" and "citation_id" in data}
    completed = events[-1][1]["citation_ids"]
    if not completed or not issued or not set(completed) <= issued:
        raise Failure("the answer has no verified citation")

    step("logout")
    client.json("POST", "/auth/logout", expect=204)
    status, _, _ = client.request("GET", "/api/v1/auth/me")
    if status != 401:
        raise Failure(f"the session survived logout: {status}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--http-url", required=True)
    parser.add_argument("--ca-file", required=True)
    parser.add_argument("--setup-token-file", required=True)
    args = parser.parse_args()
    if not args.base_url.startswith("https://") or not args.http_url.startswith("http://"):
        parser.error("--base-url must be https:// and --http-url http://")
    try:
        run(args)
    except Failure as failure:
        print(f"FAILED: {failure}", file=sys.stderr)
        return 1
    print("production smoke test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
