"""Tests for repository scripts (stdlib only): ``python3 -m unittest discover -s scripts/tests``."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import check_secrets  # noqa: E402
import precommit_lint  # noqa: E402

# Built at runtime so this file itself does not contain token-shaped literals.
GITHUB_TOKEN = "ghp_" + "A" * 36
AWS_KEY = "AKIA" + "ABCDEFGHIJKLMNOP"
ANTHROPIC_KEY = "sk-ant-" + "api03-" + "x" * 30
OPENAI_KEY = "sk-proj-" + "y" * 40
PRIVATE_KEY = "-----BEGIN " + "RSA PRIVATE KEY-----"


def rules(*lines: str) -> list[str]:
    return [f.rule for f in check_secrets.scan_lines("f.txt", enumerate(lines, start=1))]


class SecretPatternTests(unittest.TestCase):
    def test_detects_known_token_formats(self) -> None:
        self.assertEqual(rules(f"token = '{GITHUB_TOKEN}'"), ["github-token"])
        self.assertEqual(rules(f"AWS_ACCESS_KEY_ID={AWS_KEY}"), ["aws-access-key-id"])
        self.assertEqual(rules(f"ANTHROPIC_API_KEY={ANTHROPIC_KEY}"), ["anthropic-api-key"])
        self.assertEqual(rules(f"key: {OPENAI_KEY}"), ["openai-api-key"])
        self.assertEqual(rules(PRIVATE_KEY), ["private-key"])

    def test_one_finding_per_line_and_line_numbers(self) -> None:
        findings = list(check_secrets.scan_lines("a.py", [(7, f"{GITHUB_TOKEN} {AWS_KEY}")]))
        self.assertEqual([(f.path, f.line) for f in findings], [("a.py", 7)])

    def test_ordinary_code_is_not_flagged(self) -> None:
        self.assertEqual(
            rules(
                'SECRET_KEY: ${SECRET_KEY:-}',
                'password = getpass.getpass("New password: ")',
                "sk-short",
                "postgresql+asyncpg://plummy:plummy_dev_password@postgres:5432/plummy",
                "AKIA-not-a-key",
            ),
            [],
        )

    def test_allow_marker_skips_the_line(self) -> None:
        self.assertEqual(rules(f"{GITHUB_TOKEN}  # secret-scan: allow"), [])

    def test_finding_output_never_contains_the_value(self) -> None:
        (finding,) = check_secrets.scan_lines("cfg", [(1, GITHUB_TOKEN)])
        self.assertNotIn(GITHUB_TOKEN, str(finding))
        self.assertEqual(str(finding), "cfg:1: github-token")


class ForbiddenFileTests(unittest.TestCase):
    def test_env_and_key_files_are_forbidden(self) -> None:
        for path, rule in [
            (".env", "env-file"),
            ("apps/backend/.env.local", "env-file"),
            ("deploy/tls/server.KEY", "key-file"),
            ("certs/ca.pem", "key-file"),
            ("var/bootstrap/setup-token", "secret-file"),
        ]:
            with self.subTest(path=path):
                self.assertEqual(check_secrets.forbidden_file(path), rule)

    def test_examples_and_regular_files_are_allowed(self) -> None:
        for path in (".env.example", "apps/backend/src/plummy/core/security.py", "docs/keys.md"):
            with self.subTest(path=path):
                self.assertIsNone(check_secrets.forbidden_file(path))


class StagedDiffTests(unittest.TestCase):
    DIFF = "\n".join(
        [
            "diff --git a/app.py b/app.py",
            "--- a/app.py",
            "+++ b/app.py",
            "@@ -3,0 +4,2 @@",
            "+first = 1",
            f"+token = '{GITHUB_TOKEN}'",
            "@@ -10 +12 @@",
            "-old",
            "+new",
            "diff --git a/gone.txt b/gone.txt",
            "--- a/gone.txt",
            "+++ /dev/null",
            "@@ -1 +0,0 @@",
            "-removed",
        ]
    )

    def test_only_added_lines_with_their_numbers(self) -> None:
        added = check_secrets.parse_added_lines(self.DIFF)
        self.assertEqual(added, {"app.py": [(4, "first = 1"), (5, f"token = '{GITHUB_TOKEN}'"), (12, "new")]})

    def test_secret_in_added_line_is_reported_at_its_line(self) -> None:
        added = check_secrets.parse_added_lines(self.DIFF)
        findings = [f for path, lines in added.items() for f in check_secrets.scan_lines(path, lines)]
        self.assertEqual([str(f) for f in findings], ["app.py:5: github-token"])


class PrecommitSplitTests(unittest.TestCase):
    def test_files_are_routed_to_the_right_tools(self) -> None:
        backend, prettier, eslint = precommit_lint.split_staged(
            [
                "apps/backend/src/plummy/main.py",
                "apps/backend/pyproject.toml",
                "apps/frontend/src/App.tsx",
                "apps/frontend/package.json",
                "apps/frontend/node_modules/x/index.js",
                "scripts/version.py",
                "docs/README.md",
            ]
        )
        self.assertEqual(backend, ["src/plummy/main.py"])
        self.assertEqual(prettier, ["src/App.tsx", "package.json"])
        self.assertEqual(eslint, ["src/App.tsx"])

    def test_nothing_to_lint(self) -> None:
        self.assertEqual(precommit_lint.split_staged(["README.md", "VERSION"]), ([], [], []))


INIT_SECRETS = Path(__file__).resolve().parents[2] / "deploy" / "prod" / "init-secrets.sh"


@unittest.skipUnless(shutil.which("openssl"), "init-secrets.sh needs openssl")
@unittest.skipIf(os.getuid() == 0, "as root the script changes file owners")
class InitSecretsTests(unittest.TestCase):
    """deploy/prod/init-secrets.sh: two database roles (ADR-024), the upgrade of the pre-0.5.0 layout."""

    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.root)
        shutil.copy(INIT_SECRETS, self.root / "init-secrets.sh")
        self.secrets = self.root / "secrets"

    def run_script(self) -> str:
        env = {k: v for k, v in os.environ.items() if not k.startswith("PLUMMY_DB_")}
        result = subprocess.run(
            ["sh", str(self.root / "init-secrets.sh")], capture_output=True, text=True, env=env, check=True
        )
        return result.stdout + result.stderr

    def read(self, name: str) -> str:
        return (self.secrets / name).read_text()

    def test_a_new_installation_gets_both_roles(self) -> None:
        output = self.run_script()
        owner, runtime = self.read("db_password"), self.read("db_runtime_password")
        self.assertNotEqual(owner, runtime)
        self.assertEqual(
            self.read("migration_database_url"), f"postgresql+asyncpg://plummy:{owner}@postgres:5432/plummy"
        )
        self.assertEqual(
            self.read("database_url"), f"postgresql+asyncpg://plummy_app:{runtime}@postgres:5432/plummy"
        )
        for name in ("db_password", "db_runtime_password", "keyring.json", "secret_key"):
            self.assertNotIn(self.read(name), output)  # file names only

    def test_the_pre_0_5_layout_is_upgraded_and_reruns_keep_everything(self) -> None:
        self.secrets.mkdir(mode=0o700)
        old_url = "postgresql+asyncpg://plummy:oldOwnerPassword@postgres:5432/plummy"
        (self.secrets / "db_password").write_text("oldOwnerPassword")
        (self.secrets / "database_url").write_text(old_url)
        output = self.run_script()
        self.assertIn("moved", output)
        self.assertEqual(self.read("migration_database_url"), old_url)
        self.assertTrue(self.read("database_url").startswith("postgresql+asyncpg://plummy_app:"))
        before = {path.name: path.read_text() for path in self.secrets.iterdir()}
        self.assertNotIn("moved", self.run_script())
        self.assertEqual({path.name: path.read_text() for path in self.secrets.iterdir()}, before)


if __name__ == "__main__":
    unittest.main()
