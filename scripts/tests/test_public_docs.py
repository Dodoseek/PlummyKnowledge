"""Public documents: relative links resolve, anchors exist, Mermaid diagrams are well-formed (Phase 13)."""

from __future__ import annotations

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PUBLIC = ["README.md", "README.ru.md", "CHANGELOG.md", "CONTRIBUTING.md", "SECURITY.md"]
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")
FENCE = re.compile(r"(?ms)^```(\w*)\n(.*?)^```$")
DIAGRAMS = ("flowchart ", "sequenceDiagram", "stateDiagram-v2", "erDiagram", "classDiagram")


def without_code(text: str) -> str:
    return FENCE.sub("", text)


def slug(heading: str) -> str:
    """GitHub heading anchor: lower case, punctuation dropped, spaces to hyphens."""
    text = re.sub(r"[`*_]|\[([^\]]*)\]\([^)]*\)", r"\1", heading.strip().lower())
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def anchors(path: Path) -> set[str]:
    found: set[str] = set()
    for heading in re.findall(r"(?m)^#{1,6} (.+)$", without_code(path.read_text(encoding="utf-8"))):
        base, n = slug(heading), 0
        while (candidate := base if n == 0 else f"{base}-{n}") in found:
            n += 1
        found.add(candidate)
    return found


class PublicDocsTest(unittest.TestCase):
    def test_relative_links_and_anchors_resolve(self) -> None:
        for name in PUBLIC:
            source = ROOT / name
            for target in LINK.findall(without_code(source.read_text(encoding="utf-8"))):
                if re.match(r"[a-z]+:", target):
                    continue
                with self.subTest(document=name, link=target):
                    path, _, anchor = target.partition("#")
                    resolved = (source.parent / path).resolve() if path else source
                    self.assertTrue(resolved.exists(), f"{name}: no {path}")
                    if anchor and resolved.suffix == ".md":
                        self.assertIn(anchor, anchors(resolved), f"{name}: no #{anchor} in {path or name}")

    def test_mermaid_diagrams_are_well_formed(self) -> None:
        blocks = FENCE.findall((ROOT / "README.md").read_text(encoding="utf-8"))
        diagrams = [body for language, body in blocks if language == "mermaid"]
        self.assertGreaterEqual(len(diagrams), 3)
        for body in diagrams:
            with self.subTest(diagram=body.splitlines()[0]):
                self.assertTrue(body.startswith(DIAGRAMS))
                self.assertEqual(body.count("subgraph "), len(re.findall(r"(?m)^\s*end$", body)))
                self.assertNotIn("\t", body)

    def test_readmes_link_each_other(self) -> None:
        self.assertIn("(README.ru.md)", (ROOT / "README.md").read_text(encoding="utf-8"))
        self.assertIn("(README.md)", (ROOT / "README.ru.md").read_text(encoding="utf-8"))

    def test_the_slug_follows_github(self) -> None:
        self.assertEqual(slug("First steps"), "first-steps")
        self.assertEqual(slug("Команды"), "команды")
        self.assertEqual(slug("0. Среда разработки (реализовано, Фаза 1)"), "0-среда-разработки-реализовано-фаза-1")


if __name__ == "__main__":
    unittest.main()
