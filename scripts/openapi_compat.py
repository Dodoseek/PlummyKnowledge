#!/usr/bin/env python3
"""Breaking changes of the REST contract between two OpenAPI schemas (ADR-025 §2, §6).

    python3 scripts/openapi_compat.py OLD.json NEW.json
    python3 scripts/openapi_compat.py --against-release      # snapshot of the latest release tag vs working tree

Reported as breaking, unless the old operation (or parameter, property) was marked ``deprecated``:

- an operation (path + method) removed;
- a parameter removed, or a new required parameter;
- a new required property of a request body, or a property removed from a request body;
- a property removed from a successful (2xx) response;
- a response status code removed from an operation's 2xx responses.

Additions (new operations, optional parameters, response properties) are compatible. Only the standard library.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT = "apps/backend/tests/contract/openapi.snapshot.json"
METHODS = ("get", "put", "post", "delete", "patch")
RELEASE_TAG = re.compile(r"^v\d+\.\d+\.\d+$")

Schema = dict[str, Any]


def _resolve(document: Schema, node: Any, seen: frozenset[str] = frozenset()) -> Any:
    if isinstance(node, dict) and "$ref" in node:
        ref = node["$ref"]
        if ref in seen:
            return {}
        target: Any = document
        for part in ref.removeprefix("#/").split("/"):
            target = target.get(part, {}) if isinstance(target, dict) else {}
        return _resolve(document, target, seen | {ref})
    return node


def _properties(
    document: Schema, schema: Any, seen: frozenset[str] = frozenset()
) -> tuple[dict[str, Any], set[str]]:
    """Properties and required names of an object schema, following ``$ref`` and ``allOf``/``anyOf``/``oneOf``."""
    schema = _resolve(document, schema, seen)
    if not isinstance(schema, dict):
        return {}, set()
    properties = dict(schema.get("properties", {}))
    required = set(schema.get("required", []))
    for key in ("allOf", "anyOf", "oneOf"):
        for part in schema.get(key, []):
            more, more_required = _properties(document, part, seen)
            properties.update(more)
            if key == "allOf":
                required |= more_required
    return properties, required


def _body(document: Schema, operation: Schema) -> Any:
    body = _resolve(document, operation.get("requestBody", {}))
    content = body.get("content", {}) if isinstance(body, dict) else {}
    for media in content.values():
        return media.get("schema", {})
    return None


def _success(document: Schema, operation: Schema) -> dict[str, Any]:
    result = {}
    for status, response in operation.get("responses", {}).items():
        if status.startswith("2"):
            response = _resolve(document, response)
            schemas = [media.get("schema", {}) for media in response.get("content", {}).values()]
            result[status] = schemas[0] if schemas else None
    return result


def _deprecated(document: Schema, node: Any) -> bool:
    return bool(isinstance(node, dict) and _resolve(document, node).get("deprecated"))


def breaking_changes(old: Schema, new: Schema) -> list[str]:
    problems: list[str] = []
    new_paths = new.get("paths", {})
    for path, operations in old.get("paths", {}).items():
        for method in METHODS:
            if method not in operations:
                continue
            before = operations[method]
            where = f"{method.upper()} {path}"
            after = new_paths.get(path, {}).get(method)
            if after is None:
                if not before.get("deprecated"):
                    problems.append(f"{where}: operation removed without deprecation")
                continue
            problems += _parameters(old, new, where, before, after)
            problems += _request_body(old, new, where, before, after)
            problems += _responses(old, new, where, before, after)
    return problems


def _parameters(old: Schema, new: Schema, where: str, before: Schema, after: Schema) -> list[str]:
    problems = []
    old_params = {(p["in"], p["name"]): p for p in (_resolve(old, p) for p in before.get("parameters", []))}
    new_params = {(p["in"], p["name"]): p for p in (_resolve(new, p) for p in after.get("parameters", []))}
    for key, parameter in old_params.items():
        if key not in new_params and not parameter.get("deprecated"):
            problems.append(f"{where}: {key[0]} parameter '{key[1]}' removed without deprecation")
    for key, parameter in new_params.items():
        if key not in old_params and parameter.get("required"):
            problems.append(f"{where}: new required {key[0]} parameter '{key[1]}'")
        elif key in old_params and parameter.get("required") and not old_params[key].get("required"):
            problems.append(f"{where}: {key[0]} parameter '{key[1]}' became required")
    return problems


def _request_body(old: Schema, new: Schema, where: str, before: Schema, after: Schema) -> list[str]:
    old_body, new_body = _body(old, before), _body(new, after)
    if old_body is None:
        if new_body is not None and _resolve(new, after.get("requestBody", {})).get("required"):
            return [f"{where}: new required request body"]
        return []
    if new_body is None:
        return [f"{where}: request body removed"]
    problems = []
    old_props, old_required = _properties(old, old_body)
    new_props, new_required = _properties(new, new_body)
    for name in sorted(new_required - old_required):
        problems.append(f"{where}: request property '{name}' is new and required")
    for name, schema in old_props.items():
        if name not in new_props and not _deprecated(old, schema):
            problems.append(f"{where}: request property '{name}' removed without deprecation")
    return problems


def _responses(old: Schema, new: Schema, where: str, before: Schema, after: Schema) -> list[str]:
    problems = []
    old_success, new_success = _success(old, before), _success(new, after)
    for status, schema in old_success.items():
        if status not in new_success:
            problems.append(f"{where}: response {status} removed")
            continue
        if schema is None or new_success[status] is None:
            continue
        old_props, _ = _properties(old, schema)
        new_props, _ = _properties(new, new_success[status])
        for name, prop in old_props.items():
            if name not in new_props and not _deprecated(old, prop):
                problems.append(f"{where}: response {status} property '{name}' removed without deprecation")
    return problems


def latest_release_snapshot(root: Path = ROOT) -> tuple[str, Schema] | None:
    """The snapshot of the newest release tag (``vX.Y.Z``) that has one, or ``None``."""
    tags = subprocess.run(
        ["git", "tag", "--list", "v*", "--sort=-v:refname"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    for tag in (tag for tag in tags if RELEASE_TAG.match(tag)):
        shown = subprocess.run(
            ["git", "show", f"{tag}:{SNAPSHOT}"], cwd=root, capture_output=True, text=True, check=False
        )
        if shown.returncode == 0:
            return tag, json.loads(shown.stdout)
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("old", nargs="?", type=Path)
    parser.add_argument("new", nargs="?", type=Path)
    parser.add_argument("--against-release", action="store_true")
    args = parser.parse_args(argv)
    if args.against_release:
        found = latest_release_snapshot()
        if found is None:
            print("no release tag has an OpenAPI snapshot yet: nothing to compare")
            return 0
        label, old = found
        new = json.loads((ROOT / SNAPSHOT).read_text(encoding="utf-8"))
    elif args.old and args.new:
        label, old = str(args.old), json.loads(args.old.read_text(encoding="utf-8"))
        new = json.loads(args.new.read_text(encoding="utf-8"))
    else:
        parser.error("give OLD and NEW, or --against-release")
    problems = breaking_changes(old, new)
    for problem in problems:
        print(f"breaking: {problem}", file=sys.stderr)
    print(f"{len(problems)} breaking changes against {label}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
