#!/usr/bin/env python3
"""Detect secrets before they reach the repository (SECURITY_MODEL.md: secrets never in the repository).

    python3 scripts/check_secrets.py            # all tracked files (CI)
    python3 scripts/check_secrets.py --staged   # lines added in the index (pre-commit hook)

Finds private keys and well-known token formats, and files that must never be committed (``.env``,
key files, the bootstrap ``setup-token``). Matched values are never printed: only file, line and rule.
A line that is a deliberate, non-secret example can be allowed with the marker ``secret-scan: allow``.
Standard library only.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import PurePosixPath

ALLOW_MARKER = "secret-scan: allow"

RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("private-key", re.compile(r"-----BEGIN (?:[A-Z]+ )?PRIVATE KEY-----")),
    ("aws-access-key-id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github-token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})\b")),
    ("anthropic-api-key", re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai-api-key", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{32,}")),
    ("google-api-key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("slack-token", re.compile(r"\bxox[abposr]-[A-Za-z0-9-]{10,}")),
    ("stripe-live-key", re.compile(r"\b[rs]k_live_[A-Za-z0-9]{20,}")),
)

ALLOWED_ENV_FILES = frozenset({".env.example"})
FORBIDDEN_SUFFIXES = (".pem", ".key", ".p12", ".pfx")
FORBIDDEN_NAMES = frozenset({"setup-token", "id_rsa", "id_ed25519"})


@dataclass(frozen=True)
class Finding:
    path: str
    line: int | None
    rule: str

    def __str__(self) -> str:
        where = f"{self.path}:{self.line}" if self.line is not None else self.path
        return f"{where}: {self.rule}"


def forbidden_file(path: str) -> str | None:
    name = PurePosixPath(path).name
    if (name == ".env" or name.startswith(".env.")) and name not in ALLOWED_ENV_FILES:
        return "env-file"
    if name.lower().endswith(FORBIDDEN_SUFFIXES):
        return "key-file"
    if name in FORBIDDEN_NAMES:
        return "secret-file"
    return None


def scan_lines(path: str, lines: Iterable[tuple[int, str]]) -> Iterator[Finding]:
    for number, text in lines:
        if ALLOW_MARKER in text:
            continue
        for rule, pattern in RULES:
            if pattern.search(text):
                yield Finding(path, number, rule)
                break


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout


def _tracked_files() -> list[str]:
    return [p for p in _git("ls-files", "-z").split("\0") if p]


def _read_text(path: str) -> str | None:
    try:
        with open(path, "rb") as handle:
            data = handle.read()
    except OSError:
        return None
    if b"\0" in data[:8192]:
        return None  # binary
    return data.decode("utf-8", errors="replace")


def scan_tree() -> list[Finding]:
    findings: list[Finding] = []
    for path in _tracked_files():
        rule = forbidden_file(path)
        if rule is not None:
            findings.append(Finding(path, None, rule))
            continue
        text = _read_text(path)
        if text is not None:
            findings.extend(scan_lines(path, enumerate(text.splitlines(), start=1)))
    return findings


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def parse_added_lines(diff: str) -> dict[str, list[tuple[int, str]]]:
    """Map file -> (line number, text) of lines added in a unified diff with zero context."""
    added: dict[str, list[tuple[int, str]]] = {}
    path: str | None = None
    number = 0
    for raw in diff.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:]
            path = target[2:] if target.startswith("b/") else None
            continue
        match = _HUNK.match(raw)
        if match:
            number = int(match.group(1))
            continue
        if path is None:
            continue
        if raw.startswith("+"):
            added.setdefault(path, []).append((number, raw[1:]))
            number += 1
    return added


def scan_staged() -> list[Finding]:
    findings: list[Finding] = []
    names = _git("diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z").split("\0")
    for path in filter(None, names):
        rule = forbidden_file(path)
        if rule is not None:
            findings.append(Finding(path, None, rule))
    diff = _git("diff", "--cached", "--unified=0", "--no-color", "--diff-filter=ACMR", "--no-ext-diff")
    for path, lines in parse_added_lines(diff).items():
        findings.extend(scan_lines(path, lines))
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--staged", action="store_true", help="scan lines added in the index")
    args = parser.parse_args(argv)
    findings = scan_staged() if args.staged else scan_tree()
    if not findings:
        return 0
    print("secret scan: possible secrets found (values not shown):", file=sys.stderr)
    for finding in findings:
        print(f"  {finding}", file=sys.stderr)
    print(
        f"Remove them (rotate any real credential). For a deliberate non-secret example add "
        f"'{ALLOW_MARKER}' on that line.",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
