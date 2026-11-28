"""Releases carry their CHANGELOG section (ADR-025 §5): ``version.py release``/``notes`` and the release workflow."""

from __future__ import annotations

import datetime
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

import version  # noqa: E402

DATE = datetime.date(2026, 12, 1)
BASE = "https://github.com/example/project"
CHANGELOG = f"""# Changelog

Intro.

## [Unreleased]

### Added

- New thing.

### Fixed

## [0.5.0] - 2026-11-28

### Added

- Old thing.

[Unreleased]: {BASE}/compare/v0.5.0...HEAD
[0.5.0]: {BASE}/compare/v0.4.0...v0.5.0
"""


class ChangelogTest(unittest.TestCase):
    def test_release_moves_unreleased_into_a_dated_section(self) -> None:
        text = version.changelog_release(CHANGELOG, version.Version(0, 6, 0), DATE)
        self.assertIn(
            "## [Unreleased]\n\n## [0.6.0] - 2026-12-01\n\n### Added\n\n- New thing.\n\n## [0.5.0]", text
        )
        self.assertNotIn("### Fixed", text)  # an empty group is dropped
        self.assertIn(
            f"[Unreleased]: {BASE}/compare/v0.6.0...HEAD\n[0.6.0]: {BASE}/compare/v0.5.0...v0.6.0\n", text
        )
        self.assertEqual(version.changelog_notes(text, "0.6.0"), "### Added\n\n- New thing.\n")
        self.assertEqual(version.changelog_notes(text, "0.5.0"), "### Added\n\n- Old thing.\n")

    def test_release_of_the_last_section_before_the_links(self) -> None:
        text = "# Changelog\n\n## [Unreleased]\n\n- First.\n\n[Unreleased]: x\n"
        released = version.changelog_release(text, version.Version(0, 1, 0), DATE)
        self.assertEqual(version.changelog_notes(released, "0.1.0"), "- First.\n")
        self.assertTrue(released.endswith("- First.\n\n[Unreleased]: x\n"))

    def test_an_empty_unreleased_is_refused(self) -> None:
        empty = CHANGELOG.replace("- New thing.\n", "")
        with self.assertRaisesRegex(version.CommandError, "no entries"):
            version.changelog_release(empty, version.Version(0, 6, 0), DATE)

    def test_missing_or_existing_sections_are_refused(self) -> None:
        with self.assertRaisesRegex(version.CommandError, "no '## \\[Unreleased\\]'"):
            version.changelog_release("# Changelog\n", version.Version(0, 6, 0), DATE)
        with self.assertRaisesRegex(version.CommandError, "already has a section"):
            version.changelog_release(CHANGELOG, version.Version(0, 5, 0), DATE)
        with self.assertRaisesRegex(version.CommandError, "no section for 0.6.0"):
            version.changelog_notes(CHANGELOG, "0.6.0")
        with self.assertRaises(ValueError):
            version.changelog_notes(CHANGELOG, "Unreleased")

    def test_the_repository_changelog_has_every_release_tag(self) -> None:
        text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIsNotNone(re.search(r"(?m)^## \[Unreleased\]$", text))
        tags = subprocess.run(
            ["git", "tag", "--list", "v*"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.split()
        for tag in (tag for tag in tags if re.fullmatch(r"v\d+\.\d+\.\d+", tag)):
            with self.subTest(tag=tag):
                self.assertTrue(version.changelog_notes(text, tag[1:]).strip())


class ReleaseCommandTest(unittest.TestCase):
    """``version.py release`` in a throw-away repository."""

    def setUp(self) -> None:
        self.repo = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.repo)
        shutil.copy(ROOT / "scripts" / "version.py", self.repo / "version.py")
        (self.repo / "VERSION").write_text("0.6.0-dev.3\n", encoding="utf-8")
        (self.repo / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
        self.git("init", "-q")
        self.git("add", ".")
        self.git("commit", "-q", "-m", "init")

    def git(self, *args: str) -> str:
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "GIT_CONFIG_GLOBAL": os.devnull,
        }
        return subprocess.run(
            ["git", "-c", "tag.gpgSign=false", "-c", "commit.gpgSign=false", *args],
            cwd=self.repo,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout

    def run_version(self, *args: str) -> subprocess.CompletedProcess[str]:
        env = {
            **os.environ,
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_AUTHOR_NAME": "t",
            "GIT_COMMITTER_NAME": "t",
        }
        env |= {"GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_EMAIL": "t@example.invalid"}
        return subprocess.run(
            [sys.executable, "version.py", *args],
            cwd=self.repo,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def test_release_commits_the_changelog_and_tags(self) -> None:
        dry = self.run_version("release", "--dry-run")
        self.assertEqual(dry.returncode, 0, dry.stderr)
        self.assertEqual(self.git("status", "--porcelain"), "")
        result = self.run_version("release")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.git("show", "v0.6.0:VERSION"), "0.6.0\n")
        changed = self.git("show", "--name-only", "--format=", "v0.6.0^{commit}").split()
        self.assertEqual(sorted(changed), ["CHANGELOG.md", "VERSION"])
        notes = self.run_version("notes", "0.6.0")
        self.assertEqual(notes.stdout, "### Added\n\n- New thing.\n")

    def test_release_without_entries_changes_nothing(self) -> None:
        (self.repo / "CHANGELOG.md").write_text(CHANGELOG.replace("- New thing.\n", ""), encoding="utf-8")
        self.git("commit", "-q", "-am", "empty")
        for args in (("release", "--dry-run"), ("release",)):
            result = self.run_version(*args)
            self.assertEqual(result.returncode, 1)
            self.assertIn("no entries", result.stderr)
        self.assertEqual(self.git("tag"), "")
        self.assertEqual(self.git("status", "--porcelain"), "")
        self.assertEqual((self.repo / "VERSION").read_text(encoding="utf-8"), "0.6.0-dev.3\n")


class ReleaseWorkflowTest(unittest.TestCase):
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")

    def test_triggers_and_least_privilege(self) -> None:
        self.assertIn('tags: ["v*.*.*"]', self.workflow)
        self.assertIn("workflow_dispatch:", self.workflow)
        self.assertRegex(self.workflow, r"(?m)^permissions: \{\}$")
        self.assertEqual(re.findall(r"(?m)^\s+contents: (\w+)$", self.workflow), ["write"])
        self.assertIn("persist-credentials: false", self.workflow)

    def test_actions_are_pinned_by_digest(self) -> None:
        for use in re.findall(r"uses: (\S+)", self.workflow):
            with self.subTest(use=use):
                self.assertRegex(use, r"@[0-9a-f]{40}$")

    def test_inputs_reach_shell_only_through_validated_env(self) -> None:
        run_blocks = re.findall(r"(?ms)run: (?:\|\n(.*?)(?=\n      - |\Z)|(.*?)$)", self.workflow)
        for block in ("".join(parts) for parts in run_blocks):
            self.assertNotIn("${{", block)
        self.assertIn("grep -Eqx", self.workflow)
        self.assertIn('git show "$TAG:VERSION"', self.workflow)
        self.assertIn('version.py notes "${TAG#v}"', self.workflow)


if __name__ == "__main__":
    unittest.main()
