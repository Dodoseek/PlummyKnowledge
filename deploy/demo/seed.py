"""Seeds the local demo (compose.demo.yaml) through the public API. Standard library only.

On a fresh instance it creates the administrator with a random password (written to a file, never printed),
connects the built-in fake model, creates a knowledge base with the sample documents and an assistant, and then
checks that search finds them and that an answer cites them. On an instance that is already set up it does
nothing. Demo only: never point it at a real installation.
"""

from __future__ import annotations

import http.cookiejar
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

API = os.environ.get("DEMO_API_URL", "http://backend:8000").rstrip("/") + "/api/v1"
ORIGIN = os.environ.get("DEMO_ORIGIN", "http://localhost:5173")
TOKEN_FILE = Path(os.environ.get("DEMO_SETUP_TOKEN_FILE", "/bootstrap/setup-token"))
CREDENTIALS_FILE = Path(os.environ.get("DEMO_CREDENTIALS_FILE", "/demo/credentials"))
DOCUMENTS = Path(os.environ.get("DEMO_DOCUMENTS", "/demo-src/documents"))
MODEL_URL = os.environ.get("DEMO_MODEL_URL", "http://demo-llm:8080/v1")
ADMIN_EMAIL = "admin@demo.example"
QUESTION = "How much can I spend on a hotel per night?"


class SeedError(Exception):
    pass


class Api:
    def __init__(self) -> None:
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())
        )
        self.csrf: str | None = None

    def call(
        self, method: str, path: str, body: Any = None, *, raw: bytes | None = None, content_type: str = ""
    ) -> tuple[int, bytes]:
        headers = {"Origin": ORIGIN, "Accept": "application/json, text/event-stream"}
        data = raw
        if body is not None:
            data, content_type = json.dumps(body).encode(), "application/json"
        if content_type:
            headers["Content-Type"] = content_type
        if self.csrf and method != "GET":
            headers["X-CSRF-Token"] = self.csrf
        request = urllib.request.Request(API + path, data=data, method=method, headers=headers)  # noqa: S310
        try:
            with self.opener.open(request, timeout=120) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as error:
            return error.code, error.read()

    def json(self, method: str, path: str, body: Any = None, *, expect: int = 200) -> Any:
        status, content = self.call(method, path, body)
        if status != expect:
            raise SeedError(f"{method} {path}: {status} {content[:300]!r}")
        return json.loads(content) if content else None


def wait_ready(api: Api) -> None:
    deadline = time.monotonic() + 300
    while api.call("GET", "/health/ready")[0] != 200:
        if time.monotonic() > deadline:
            raise SeedError("the API did not become ready")
        time.sleep(2)


def initialize(api: Api) -> None:
    token = TOKEN_FILE.read_text(encoding="utf-8").strip()
    password = "Demo-" + secrets.token_urlsafe(15)
    session = api.json(
        "POST",
        "/setup/initialize",
        {
            "bootstrap_token": token,
            "admin": {"email": ADMIN_EMAIL, "display_name": "Demo Admin", "password": password},
        },
        expect=201,
    )
    api.csrf = session["csrf_token"]
    CREDENTIALS_FILE.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(CREDENTIALS_FILE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as file:
        file.write(f"URL:      {ORIGIN}\nEmail:    {ADMIN_EMAIL}\nPassword: {password}\n")


def upload(api: Api, kb_id: str, path: Path) -> str:
    boundary = uuid.uuid4().hex
    body = (
        (
            f'--{boundary}\r\nContent-Disposition: form-data; name="knowledge_base_id"\r\n\r\n{kb_id}\r\n'
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{path.name}"\r\n'
            "Content-Type: text/markdown\r\n\r\n"
        ).encode()
        + path.read_bytes()
        + f"\r\n--{boundary}--\r\n".encode()
    )
    status, content = api.call(
        "POST", "/documents", raw=body, content_type=f"multipart/form-data; boundary={boundary}"
    )
    if status not in (201, 202):
        raise SeedError(f"upload {path.name}: {status} {content[:300]!r}")
    return str(json.loads(content)["document"]["id"])


def wait_indexed(api: Api, document_ids: list[str]) -> None:
    deadline = time.monotonic() + 300
    pending = set(document_ids)
    while pending:
        for document_id in list(pending):
            status = api.json("GET", f"/documents/{document_id}")["processing_status"]
            if status == "indexed":
                pending.discard(document_id)
            elif status in {"failed", "deleted"}:
                raise SeedError(f"document {document_id} is {status}")
        if pending and time.monotonic() > deadline:
            raise SeedError("indexing did not finish")
        time.sleep(1)


def answer_cites_a_document(api: Api, assistant_id: str) -> bool:
    status, content = api.call(
        "POST",
        "/chat/stream",
        {"assistant_id": assistant_id, "content": QUESTION, "client_message_id": str(uuid.uuid4())},
    )
    if status != 200:
        raise SeedError(f"chat: {status} {content[:300]!r}")
    events = []
    for block in content.decode().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in fields and "data" in fields:
            events.append((fields["event"], json.loads(fields["data"])))
    if not events or events[-1][0] != "message_complete":
        return False
    issued = {data.get("citation_id") for name, data in events if name == "citation"}
    cited = set(events[-1][1].get("citation_ids") or [])
    return bool(cited) and cited <= issued


def seed(api: Api) -> None:
    provider = api.json(
        "POST",
        "/providers",
        {
            "name": "Demo model (built-in fake)",
            "provider_type": "openai_compatible",
            "base_url": MODEL_URL,
            "settings": {"timeout_seconds": 30},
            "enabled": True,
        },
        expect=201,
    )
    if api.json("POST", f"/providers/{provider['id']}/test")["status"] != "ok":
        raise SeedError("the demo model did not pass the connection test")
    model = api.json(
        "POST",
        f"/providers/{provider['id']}/models",
        {"model_id": "fake-chat", "context_window": 32000, "max_output_tokens": 1024},
        expect=201,
    )
    profile = api.json(
        "POST",
        "/embedding-profiles",
        {
            "name": "Demo embeddings",
            "provider_id": provider["id"],
            "model_id": "fake-embed",
            "dimensions": 64,
            "distance_metric": "cosine",
            "normalize": True,
        },
        expect=201,
    )
    kb = api.json(
        "POST",
        "/knowledge-bases",
        {
            "name": "Company handbook",
            "description": "Sample policies of a fictional company.",
            "embedding_profile_id": profile["id"],
        },
        expect=201,
    )
    documents = sorted(DOCUMENTS.glob("*.md"))
    if not documents:
        raise SeedError(f"no sample documents in {DOCUMENTS}")
    wait_indexed(api, [upload(api, kb["id"], path) for path in documents])
    assistant = api.json(
        "POST",
        "/assistants",
        {
            "name": "Handbook assistant",
            "description": "Answers questions about the company handbook.",
            "instructions": "Answer from the handbook only and cite the passages you use.",
            "model_config_id": model["id"],
            "embedding_profile_id": profile["id"],
            "knowledge_base_ids": [kb["id"]],
        },
        expect=201,
    )
    hits = api.json(
        "POST", f"/knowledge-bases/{kb['id']}/search", {"query": "hotel per night", "mode": "hybrid"}
    )
    if not hits["items"]:
        raise SeedError("search found nothing in the sample documents")
    if not answer_cites_a_document(api, assistant["id"]):
        raise SeedError("the answer has no citation")


def main() -> int:
    api = Api()
    try:
        wait_ready(api)
        if api.json("GET", "/setup/status")["status"] != "uninitialized":
            print("The demo is already set up.")
            return 0
        initialize(api)
        seed(api)
    except (SeedError, OSError, KeyError, ValueError) as error:
        print(f"demo seed failed: {error}", file=sys.stderr)
        return 1
    print("The demo is ready: a knowledge base with sample documents and an assistant.")
    print(f"Open {ORIGIN}. The administrator's sign-in details are in {CREDENTIALS_FILE} (see the README).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
