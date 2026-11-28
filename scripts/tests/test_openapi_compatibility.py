"""The REST contract keeps compatibility with the last release (ADR-025 §2, §6).

The working tree's OpenAPI snapshot is compared with the snapshot of the newest release tag that has one. The
comparator itself is tested on small schemas, so a passing comparison means something.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import unittest
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("openapi_compat", ROOT / "scripts" / "openapi_compat.py")
assert spec is not None and spec.loader is not None
compat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compat)

BASE: dict[str, Any] = {
    "paths": {
        "/api/v1/items/{item_id}": {
            "get": {
                "parameters": [
                    {"in": "path", "name": "item_id", "required": True},
                    {"in": "query", "name": "expand", "required": False},
                ],
                "responses": {
                    "200": {
                        "content": {"application/json": {"schema": {"$ref": "#/components/schemas/Item"}}}
                    }
                },
            },
            "patch": {
                "requestBody": {
                    "required": True,
                    "content": {"application/json": {"schema": {"$ref": "#/components/schemas/ItemUpdate"}}},
                },
                "responses": {"204": {"description": "done"}},
            },
        }
    },
    "components": {
        "schemas": {
            "Item": {
                "properties": {"id": {}, "name": {}, "legacy": {"deprecated": True}},
                "required": ["id"],
            },
            "ItemUpdate": {"properties": {"name": {}, "note": {}}},
        }
    },
}


def changed(edit: Any) -> list[str]:
    new = copy.deepcopy(BASE)
    edit(new)
    result: list[str] = compat.breaking_changes(BASE, new)
    return result


class ComparatorTest(unittest.TestCase):
    def test_identical_schemas_are_compatible(self) -> None:
        self.assertEqual(compat.breaking_changes(BASE, copy.deepcopy(BASE)), [])

    def test_additions_are_compatible(self) -> None:
        def add(schema: dict[str, Any]) -> None:
            schema["paths"]["/api/v1/other"] = {"get": {"responses": {"200": {}}}}
            schema["components"]["schemas"]["Item"]["properties"]["extra"] = {}
            schema["paths"]["/api/v1/items/{item_id}"]["get"]["parameters"].append(
                {"in": "query", "name": "fields", "required": False}
            )

        self.assertEqual(changed(add), [])

    def test_removed_operation(self) -> None:
        problems = changed(lambda s: s["paths"]["/api/v1/items/{item_id}"].pop("patch"))
        self.assertEqual(problems, ["PATCH /api/v1/items/{item_id}: operation removed without deprecation"])

    def test_removed_deprecated_operation_is_allowed(self) -> None:
        old = copy.deepcopy(BASE)
        old["paths"]["/api/v1/items/{item_id}"]["patch"]["deprecated"] = True
        new = copy.deepcopy(BASE)
        new["paths"]["/api/v1/items/{item_id}"].pop("patch")
        self.assertEqual(compat.breaking_changes(old, new), [])

    def test_parameters(self) -> None:
        def remove(schema: dict[str, Any]) -> None:
            schema["paths"]["/api/v1/items/{item_id}"]["get"]["parameters"].pop()

        def require(schema: dict[str, Any]) -> None:
            schema["paths"]["/api/v1/items/{item_id}"]["get"]["parameters"][1]["required"] = True

        self.assertIn("parameter 'expand' removed", changed(remove)[0])
        self.assertIn("'expand' became required", changed(require)[0])

    def test_request_body(self) -> None:
        def require(schema: dict[str, Any]) -> None:
            schema["components"]["schemas"]["ItemUpdate"]["required"] = ["name"]

        def remove(schema: dict[str, Any]) -> None:
            del schema["components"]["schemas"]["ItemUpdate"]["properties"]["note"]

        self.assertIn("request property 'name' is new and required", changed(require)[0])
        self.assertIn("request property 'note' removed", changed(remove)[0])

    def test_response_properties(self) -> None:
        def remove(schema: dict[str, Any]) -> None:
            properties = schema["components"]["schemas"]["Item"]["properties"]
            del properties["name"]
            del properties["legacy"]  # deprecated: may go

        self.assertEqual(
            changed(remove),
            ["GET /api/v1/items/{item_id}: response 200 property 'name' removed without deprecation"],
        )

    def test_removed_success_status(self) -> None:
        def move(schema: dict[str, Any]) -> None:
            responses = schema["paths"]["/api/v1/items/{item_id}"]["patch"]["responses"]
            responses["200"] = responses.pop("204")

        self.assertEqual(changed(move), ["PATCH /api/v1/items/{item_id}: response 204 removed"])


class AgainstReleaseTest(unittest.TestCase):
    def test_the_snapshot_is_compatible_with_the_latest_release(self) -> None:
        found = compat.latest_release_snapshot()
        if found is None:
            self.skipTest("no release tag has an OpenAPI snapshot yet")
        tag, old = found
        new = json.loads((ROOT / compat.SNAPSHOT).read_text(encoding="utf-8"))
        self.assertEqual(
            compat.breaking_changes(old, new), [], f"breaking changes against {tag} (ADR-025 §2)"
        )


if __name__ == "__main__":
    unittest.main()
