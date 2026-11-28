"""The OpenAPI schema of the API equals the committed snapshot (ADR-025 §6).

Any change to the public REST contract shows up in the diff of ``openapi.snapshot.json``. Update the snapshot
in the same commit as the change (from the repository root; ``tests`` is mounted read-only in the container):

    docker compose run --rm --no-deps -v "$PWD/apps/backend:/work" -w /work -e UPDATE_OPENAPI_SNAPSHOT=1 \
        backend pytest tests/contract/test_openapi_snapshot.py

Compatibility with the last release (nothing removed without ``deprecated``) is checked against the snapshot
of that release tag by ``scripts/tests/test_openapi_compatibility.py``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from tests.conftest import build_test_app

SNAPSHOT = Path(__file__).with_name("openapi.snapshot.json")


def current_schema() -> dict[str, Any]:
    app, _ = build_test_app()
    schema: dict[str, Any] = json.loads(json.dumps(app.openapi()))
    schema["info"]["version"] = "snapshot"  # VERSION changes on every commit (ADR-009)
    return schema


def render(schema: dict[str, Any]) -> str:
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def test_the_schema_matches_the_snapshot() -> None:
    text = render(current_schema())
    if os.environ.get("UPDATE_OPENAPI_SNAPSHOT") == "1":
        SNAPSHOT.write_text(text, encoding="utf-8")
    assert SNAPSHOT.exists(), "no snapshot: run with UPDATE_OPENAPI_SNAPSHOT=1"
    assert SNAPSHOT.read_text(encoding="utf-8") == text, (
        "the API contract changed: review the change, update docs/API_CONTRACTS.md and CHANGELOG.md, "
        "then update it with UPDATE_OPENAPI_SNAPSHOT=1 (see this module's docstring)"
    )


def test_every_operation_is_described() -> None:
    for path, operations in current_schema()["paths"].items():
        for method, operation in operations.items():
            assert operation.get("operationId"), (method, path)
            assert operation.get("responses"), (method, path)
