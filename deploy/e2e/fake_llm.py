"""Fake OpenAI-compatible provider for the end-to-end tests (compose.e2e.yaml) and the local demo
(compose.demo.yaml). Test and demo infrastructure only.

Standard library only, deterministic, no network access:

- ``GET /v1/models`` lists one chat and one embedding model (connection test);
- ``POST /v1/embeddings`` returns hashed bag-of-words vectors (texts sharing words are close);
- ``POST /v1/chat/completions`` streams an answer built from the first ``<document>`` of the prompt and cites
  it. The last user message steers it: ``slowly`` streams one word every 0.3 s (to test stopping),
  ``invent`` also cites an ID that was never issued (the backend must drop it);
- ``GET /_chat-log`` returns the text of every chat request received, so a test can prove that a document never
  reached the model (SECURITY_MODEL.md SC-01). Embedding requests are not logged: indexing sends every document.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

CHAT_MODEL = "fake-chat"
EMBEDDING_MODEL = "fake-embed"
DIMENSIONS = 64
DOCUMENT = re.compile(r'<document id="(c\d+)"[^>]*>\n(.*?)\n</document>', re.DOTALL)
WORD = re.compile(r"\w+", re.UNICODE)
MAX_BODY = 8 * 1024 * 1024

_log: list[str] = []
_lock = threading.Lock()


def embed(text: str) -> list[float]:
    vector = [0.0] * DIMENSIONS
    for word in WORD.findall(text.lower()):
        digest = hashlib.sha256(word.encode()).digest()
        vector[digest[0] % DIMENSIONS] += 1.0 if digest[1] % 2 else -1.0
    if not any(vector):  # words cancelled out (or no words): the backend rejects zero vectors
        vector[0] = 1.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


def last_question(messages: list[dict[str, Any]]) -> str:
    return next((str(m.get("content", "")) for m in reversed(messages) if m.get("role") == "user"), "").lower()


def answer_words(messages: list[dict[str, Any]]) -> list[str]:
    prompt = "\n".join(str(message.get("content", "")) for message in messages)
    question = last_question(messages)
    documents = DOCUMENT.findall(prompt)
    if not documents:
        return "I could not find this in the knowledge base.".split(" ")
    citation, text = documents[0]
    first_sentence = re.split(r"(?<=[.!?])\s", " ".join(text.split()))[0][:300]
    words = f"According to the documents: {first_sentence} [{citation}]".split(" ")
    if "invent" in question:
        words += ["See", "also", "[c42]."]
    if "slowly" in question:
        words += [f"filler{index}" for index in range(60)]
    return words


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - signature of the base class
        return  # prompts are test data, but keep the output quiet

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            raise ValueError("body too large")
        data = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(data, dict):
            raise ValueError("body must be an object")
        return data

    def do_GET(self) -> None:
        if self.path == "/v1/models":
            self._json(200, {"object": "list", "data": [{"id": CHAT_MODEL}, {"id": EMBEDDING_MODEL}]})
        elif self.path == "/_chat-log":
            with _lock:
                self._json(200, {"requests": list(_log)})
        elif self.path == "/health":
            self._json(200, {"status": "ok"})
        else:
            self._json(404, {"error": {"message": "not found"}})

    def do_POST(self) -> None:
        try:
            body = self._body()
        except ValueError:
            self._json(400, {"error": {"message": "bad request"}})
            return
        if self.path == "/v1/embeddings":
            texts = body.get("input")
            if isinstance(texts, str):
                texts = [texts]
            if not isinstance(texts, list):
                self._json(400, {"error": {"message": "input must be a list"}})
                return
            data = [{"object": "embedding", "index": i, "embedding": embed(str(t))} for i, t in enumerate(texts)]
            self._json(200, {"object": "list", "data": data, "model": EMBEDDING_MODEL})
        elif self.path == "/v1/chat/completions":
            messages = body.get("messages")
            if not isinstance(messages, list):
                self._json(400, {"error": {"message": "messages must be a list"}})
                return
            with _lock:
                _log.append("\n".join(str(m.get("content", "")) for m in messages if isinstance(m, dict)))
            self._stream([m for m in messages if isinstance(m, dict)])
        else:
            self._json(404, {"error": {"message": "not found"}})

    def _stream(self, messages: list[dict[str, Any]]) -> None:
        words = answer_words(messages)
        slow = "slowly" in last_question(messages)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def send(payload: Any) -> None:
            self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
            self.wfile.flush()

        try:
            for index, word in enumerate(words):
                delta = word if index == 0 else f" {word}"
                send({"choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}]})
                time.sleep(0.3 if slow else 0.01)
            send({"choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]})
            send({"choices": [], "usage": {"prompt_tokens": 100, "completion_tokens": len(words)}})
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            return  # the backend stopped the generation


def main() -> None:
    server = ThreadingHTTPServer(("0.0.0.0", 8080), Handler)  # noqa: S104 - inside the e2e network only
    server.daemon_threads = True
    server.serve_forever()


if __name__ == "__main__":
    main()
