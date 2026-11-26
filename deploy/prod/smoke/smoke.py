"""Smoke test of the production stack through Nginx (ADR-023; CI job "Production stack").

Standard library only. Runs on the host against the published ports:

    python3 deploy/prod/smoke/smoke.py run --base-url https://localhost:8443 \
        --http-url http://localhost:8080 --ca-file <cert.pem> --setup-token-file <file> --state-file <file>
    python3 deploy/prod/smoke/smoke.py delete-document --base-url ... --ca-file ... --state-file <file>
    python3 deploy/prod/smoke/smoke.py verify-restore --base-url ... --ca-file ... --state-file <file>

``run`` checks the HTTP → HTTPS redirect, the security headers and CSP, readiness, that Swagger UI is off,
then the core scenario as an administrator would run it: setup → provider (the fake OpenAI-compatible server)
→ models → embedding profile → knowledge base → two uploads → indexed → search → assistant → streamed answer
with a verified citation → logout. The identifiers and the generated password go to the state file (0600).

The restore drill (DEPLOYMENT.md §10) continues from there: backup → ``delete-document`` (the second document,
deleted after the dump) → export of the deletions → the volumes are dropped → restore → ``verify-restore``:
the kept document is found and reindexed from the restored stored object, the deleted one is gone again, the
answer still carries a verified citation. Prints step names only: no tokens, passwords or document text.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
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
DELETED_LATER = """# Parking policy

## Garage

Visitors park in the north garage. Parking permits are issued by the reception desk.
"""
ADMIN_EMAIL = "smoke-admin@example.com"


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


def upload(client: Client, kb_id: str, filename: str, text: str) -> str:
    body, content_type = multipart({"knowledge_base_id": kb_id}, filename, text.encode())
    status, _, content = client.request("POST", "/api/v1/documents", raw=body, content_type=content_type)
    if status not in (201, 202):
        raise Failure(f"upload: {status} {content[:300]!r}")
    return str(json.loads(content)["document"]["id"])


def wait_indexed(client: Client, document_id: str) -> None:
    deadline = time.monotonic() + 240
    while True:
        state = client.json("GET", f"/documents/{document_id}")["processing_status"]
        if state == "indexed":
            return
        if state == "failed" or time.monotonic() > deadline:
            raise Failure(f"document is {state}")
        time.sleep(2)


def wait_job(client: Client, job_id: str) -> None:
    deadline = time.monotonic() + 240
    while True:
        job = client.json("GET", f"/jobs/ingestion/{job_id}")
        if job["status"] == "succeeded":
            return
        if job["status"] in ("failed", "cancelled", "dead") or time.monotonic() > deadline:
            code = (job.get("error") or {}).get("code")
            raise Failure(f"job is {job['status']} ({code})")
        time.sleep(2)


def search_hits(client: Client, kb_id: str, query: str) -> set[str]:
    found = client.json("POST", f"/knowledge-bases/{kb_id}/search", {"query": query, "mode": "hybrid"})
    return {hit["document_id"] for hit in found["items"]}


def answer_with_citation(client: Client, assistant_id: str) -> None:
    status, headers, content = client.request(
        "POST",
        "/api/v1/chat/stream",
        {
            "assistant_id": assistant_id,
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


def write_state(path: str, state: dict[str, str]) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        json.dump(state, file)


def read_state(path: str) -> dict[str, str]:
    with open(path, encoding="utf-8") as file:
        state: dict[str, str] = json.load(file)
    return state


def login(args: argparse.Namespace, state: dict[str, str]) -> Client:
    client = Client(args.base_url.rstrip("/"), ssl.create_default_context(cafile=args.ca_file))
    deadline = time.monotonic() + 120
    while True:
        try:
            if client.request("GET", "/api/v1/health/ready")[0] == 200:
                break
        except urllib.error.URLError:
            pass  # Nginx is not listening yet
        if time.monotonic() > deadline:
            raise Failure("the API is not ready")
        time.sleep(2)
    session = client.json("POST", "/auth/login", {"email": ADMIN_EMAIL, "password": state["password"]})
    client.csrf = session["csrf_token"]
    return client


def delete_document(args: argparse.Namespace) -> None:
    state = read_state(args.state_file)
    client = login(args, state)
    step("delete the second document (after the backup)")
    client.json("DELETE", f"/documents/{state['deleted_document_id']}", expect=204)
    if state["deleted_document_id"] in search_hits(client, state["knowledge_base_id"], "parking permits"):
        raise Failure("the deleted document is still found")


def verify_restore(args: argparse.Namespace) -> None:
    state = read_state(args.state_file)
    step("sign in on the restored instance")
    client = login(args, state)
    kb_id = state["knowledge_base_id"]

    step("the document deleted after the backup stays deleted")
    status, _, _ = client.request("GET", f"/api/v1/documents/{state['deleted_document_id']}")
    if status != 404:
        raise Failure(f"the deleted document is back: {status}")
    if state["deleted_document_id"] in search_hits(client, kb_id, "parking permits"):
        raise Failure("the deleted document is found again")

    step("the kept document is found and reindexed from the restored stored object")
    if state["kept_document_id"] not in search_hits(client, kb_id, "hotel limit"):
        raise Failure("the kept document is not found")
    reindex = client.json("POST", f"/documents/{state['kept_document_id']}/reindex", expect=202)
    wait_job(client, reindex["job"]["id"])
    wait_indexed(client, state["kept_document_id"])

    step("streamed answer with a citation")
    answer_with_citation(client, state["assistant_id"])
    client.json("POST", "/auth/logout", expect=204)


def run(args: argparse.Namespace) -> None:
    context = ssl.create_default_context(cafile=args.ca_file)
    client = Client(args.base_url.rstrip("/"), context)

    step("HTTP redirects to HTTPS")
    plain = urllib.request.build_opener(NoRedirect())
    try:
        plain.open(args.http_url.rstrip("/") + "/knowledge-bases?x=1", timeout=10)
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
    with open(args.setup_token_file, encoding="utf-8") as file:
        token = file.read().strip()
    if client.json("GET", "/setup/status")["status"] != "uninitialized":
        raise Failure("the instance is already initialized (use a fresh stack)")
    password = "Smoke-" + secrets.token_urlsafe(18)
    session = client.json(
        "POST",
        "/setup/initialize",
        {
            "bootstrap_token": token,
            "admin": {
                "email": ADMIN_EMAIL,
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
    document_id = upload(client, kb["id"], "travel.md", DOCUMENT)
    deleted_later_id = upload(client, kb["id"], "parking.md", DELETED_LATER)
    wait_indexed(client, document_id)
    wait_indexed(client, deleted_later_id)

    step("search")
    found = client.json(
        "POST", f"/knowledge-bases/{kb['id']}/search", {"query": "hotel limit", "mode": "hybrid"}
    )
    if document_id not in {hit["document_id"] for hit in found["items"]}:
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
    answer_with_citation(client, assistant["id"])

    step("logout")
    client.json("POST", "/auth/logout", expect=204)
    status, _, _ = client.request("GET", "/api/v1/auth/me")
    if status != 401:
        raise Failure(f"the session survived logout: {status}")

    write_state(
        args.state_file,
        {
            "password": password,
            "knowledge_base_id": kb["id"],
            "assistant_id": assistant["id"],
            "kept_document_id": document_id,
            "deleted_document_id": deleted_later_id,
        },
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "delete-document", "verify-restore"):
        command = commands.add_parser(name)
        command.add_argument("--base-url", required=True)
        command.add_argument("--ca-file", required=True)
        command.add_argument("--state-file", required=True)
        if name == "run":
            command.add_argument("--http-url", required=True)
            command.add_argument("--setup-token-file", required=True)
    args = parser.parse_args()
    if not args.base_url.startswith("https://") or not getattr(args, "http_url", "http://").startswith(
        "http://"
    ):
        parser.error("--base-url must be https:// and --http-url http://")
    actions = {"run": run, "delete-document": delete_document, "verify-restore": verify_restore}
    try:
        actions[args.command](args)
    except Failure as failure:
        print(f"FAILED: {failure}", file=sys.stderr)
        return 1
    print(f"{args.command}: passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
