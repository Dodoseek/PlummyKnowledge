"""docs/API_CONTRACTS.md describes exactly the implemented REST operations (ADR-025 §1).

Every operation of the OpenAPI snapshot has a row in a table of API_CONTRACTS.md, and every row names an
implemented operation unless its description starts with **Вне 1.0** (not implemented, outside the 1.0 contract).
"""

from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "API_CONTRACTS.md"
SNAPSHOT = ROOT / "apps" / "backend" / "tests" / "contract" / "openapi.snapshot.json"
ROW = re.compile(r"^\|\s*([A-Z][A-Z /]*?)\s*\|\s*`(/api/v1[^`]*)`\s*\|(.*)$")
OUTSIDE = "**Вне 1.0"


def normalized(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "{}", path)


def implemented() -> set[tuple[str, str]]:
    paths = json.loads(SNAPSHOT.read_text(encoding="utf-8"))["paths"]
    return {(method.upper(), normalized(path)) for path, operations in paths.items() for method in operations}


def documented() -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
    """(rows of the contract, rows marked outside 1.0)."""
    contract: set[tuple[str, str]] = set()
    outside: set[tuple[str, str]] = set()
    for line in DOC.read_text(encoding="utf-8").splitlines():
        match = ROW.match(line)
        if not match:
            continue
        for method in match.group(1).split("/"):
            key = (method.strip(), normalized(match.group(2)))
            description = match.group(3).split("|", 1)[0].strip()
            (outside if description.startswith(OUTSIDE) else contract).add(key)
    return contract, outside


class ApiContractsDocTest(unittest.TestCase):
    def test_every_implemented_operation_is_documented(self) -> None:
        contract, _ = documented()
        self.assertEqual(sorted(implemented() - contract), [])

    def test_every_documented_operation_is_implemented_or_outside_1_0(self) -> None:
        contract, _ = documented()
        self.assertEqual(sorted(contract - implemented()), [])

    def test_rows_outside_1_0_are_not_implemented(self) -> None:
        _, outside = documented()
        self.assertTrue(outside)
        self.assertEqual(sorted(outside & implemented()), [], "implemented: remove the **Вне 1.0** mark")


if __name__ == "__main__":
    unittest.main()
