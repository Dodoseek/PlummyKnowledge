"""The traceability of SC-01…SC-09 points at tests that exist (SECURITY_MODEL §18.2).

Runs with the repository checkout (CI job "Repository"), where backend, frontend and E2E tests are all present.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "apps" / "backend"


def load_scenarios() -> dict[str, dict[str, list[str]]]:
    spec = importlib.util.spec_from_file_location("security_scenarios", BACKEND / "tests" / "security_scenarios.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    scenarios: dict[str, dict[str, list[str]]] = module.SCENARIOS
    return scenarios


def python_tests(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test_")
    }


def script_tests(path: Path) -> set[str]:
    text = path.read_text(encoding="utf-8")
    return {match.group(2) for match in re.finditer(r"\b(?:test|it)\(\s*(['\"`])(.*?)\1", text)}


class SecurityScenarioTests(unittest.TestCase):
    def test_every_scenario_of_the_security_model_is_traced(self) -> None:
        model = (ROOT / "docs" / "SECURITY_MODEL.md").read_text(encoding="utf-8")
        documented = set(re.findall(r"^\| (SC-\d\d) \|", model, flags=re.MULTILINE))
        self.assertEqual(set(load_scenarios()), documented)

    def test_every_clause_has_tests_and_every_test_exists(self) -> None:
        missing: list[str] = []
        for scenario, clauses in load_scenarios().items():
            for clause, references in clauses.items():
                self.assertTrue(references, f"{scenario}: {clause}")
                for reference in references:
                    file_part, _, name = reference.partition("::")
                    path = BACKEND / file_part if file_part.startswith("tests/") else ROOT / file_part
                    if not path.is_file():
                        missing.append(reference)
                        continue
                    found = python_tests(path) if path.suffix == ".py" else script_tests(path)
                    if name not in found:
                        missing.append(reference)
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
