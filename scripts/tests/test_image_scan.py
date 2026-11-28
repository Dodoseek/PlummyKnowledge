"""The container scan policy (SECURITY_MODEL §19, Phase 12.6): a pinned scanner and reviewed exceptions.

Every exception in deploy/trivy/<image>.trivyignore names one finding, gives the reason in a comment above it
and expires within a year, after which Trivy reports the finding again. Exceptions are for third-party images
only: our own images are rebuilt instead.
"""

from __future__ import annotations

import datetime
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "deploy" / "scripts" / "scan-images.sh"
IGNORES = ROOT / "deploy" / "trivy"
ENTRY = re.compile(r"^(CVE-\d{4}-\d{4,}|GHSA(-[23456789cfghjmpqrvwx]{4}){3}) exp:(\d{4}-\d{2}-\d{2})$")
MAX_EXPIRY = datetime.timedelta(days=366)


def third_party_images() -> dict[str, str]:
    """Image name (last path segment, no tag) → reference, for the images compose.prod.yaml pulls."""
    text = (ROOT / "compose.prod.yaml").read_text(encoding="utf-8")
    images = re.findall(r"^\s+image:\s*(\S+)\s*$", text, flags=re.MULTILINE)
    return {ref.split("@")[0].rsplit(":", 1)[0].rsplit("/", 1)[-1]: ref for ref in images if "$" not in ref}


class ImageScanTest(unittest.TestCase):
    def test_the_scanner_is_pinned_by_digest(self) -> None:
        text = SCRIPT.read_text(encoding="utf-8")
        self.assertRegex(text, r"aquasec/trivy:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}")
        for flag in ("--severity CRITICAL,HIGH", "--ignore-unfixed", "--exit-code 1"):
            self.assertIn(flag, text)

    def test_ci_scans_the_production_images(self) -> None:
        workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertIn("sh deploy/scripts/scan-images.sh", workflow)

    def test_exceptions_are_for_third_party_images_only(self) -> None:
        images = third_party_images()
        self.assertIn("pgvector", images)
        for path in IGNORES.glob("*"):
            self.assertEqual(path.suffix, ".trivyignore", path.name)
            self.assertIn(path.stem, images, f"{path.name}: not an image of compose.prod.yaml")

    def test_every_exception_has_a_reason_and_an_expiry(self) -> None:
        today = datetime.date.today()
        files = sorted(IGNORES.glob("*.trivyignore"))
        self.assertTrue(files)
        for path in files:
            reason = False
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                where = f"{path.name}:{number}"
                if not line.strip():
                    reason = False  # a blank line ends a block: the next entries need their own reason
                    continue
                if line.startswith("#"):
                    reason = True
                    continue
                match = ENTRY.match(line)
                self.assertIsNotNone(match, f"{where}: expected '<CVE or GHSA id> exp:YYYY-MM-DD'")
                assert match is not None
                self.assertTrue(reason, f"{where}: no reason in a comment above the entry")
                expiry = datetime.date.fromisoformat(match.group(3))
                self.assertLessEqual(expiry - today, MAX_EXPIRY, f"{where}: expires more than a year ahead")

    def test_ids_are_not_repeated(self) -> None:
        for path in IGNORES.glob("*.trivyignore"):
            ids = [line.split()[0] for line in path.read_text(encoding="utf-8").splitlines() if ENTRY.match(line)]
            self.assertEqual(len(ids), len(set(ids)), path.name)


if __name__ == "__main__":
    unittest.main()
