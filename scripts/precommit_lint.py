#!/usr/bin/env python3
"""Fast lint of staged files, run by ``.githooks/pre-commit`` before the version bump.

- backend ``*.py`` under ``apps/backend``: ``ruff check`` and ``ruff format --check`` (via ``uv run``);
- frontend files under ``apps/frontend``: ``prettier --check``; ``eslint`` for TypeScript/JavaScript.

A tool that is not installed locally is skipped with a notice (CI runs the full checks anyway). The files are
checked as they are in the working tree. ``PLUMMY_SKIP_LINT=1`` skips this step once; the secret scan and the
version bump still run. Standard library only.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

BACKEND = PurePosixPath("apps/backend")
FRONTEND = PurePosixPath("apps/frontend")
PRETTIER_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".json", ".css", ".html")
ESLINT_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")


def split_staged(paths: Sequence[str]) -> tuple[list[str], list[str], list[str]]:
    """(backend python files, frontend prettier files, frontend eslint files), relative to each app."""
    backend: list[str] = []
    prettier: list[str] = []
    eslint: list[str] = []
    for raw in paths:
        path = PurePosixPath(raw)
        if path.is_relative_to(BACKEND) and path.suffix == ".py":
            backend.append(str(path.relative_to(BACKEND)))
        elif path.is_relative_to(FRONTEND) and "node_modules" not in path.parts:
            relative = str(path.relative_to(FRONTEND))
            if path.suffix in PRETTIER_SUFFIXES:
                prettier.append(relative)
            if path.suffix in ESLINT_SUFFIXES:
                eslint.append(relative)
    return backend, prettier, eslint


def _staged() -> list[str]:
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"],
        check=True,
        capture_output=True,
        text=True,
    )
    return [p for p in result.stdout.split("\0") if p]


def _run(command: list[str], cwd: Path) -> bool:
    return subprocess.run(command, cwd=cwd).returncode == 0


def main() -> int:
    if os.environ.get("PLUMMY_SKIP_LINT") == "1":
        print("pre-commit: lint skipped (PLUMMY_SKIP_LINT=1)", file=sys.stderr)
        return 0
    toplevel = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"], check=True, capture_output=True, text=True
    ).stdout.strip()
    root = Path(toplevel)
    backend, prettier, eslint = split_staged(_staged())
    ok = True

    if backend:
        if shutil.which("uv") is None:
            print("pre-commit: uv not found, skipping ruff (CI will run it)", file=sys.stderr)
        else:
            cwd = root / BACKEND
            ok &= _run(["uv", "run", "--frozen", "ruff", "check", *backend], cwd)
            ok &= _run(["uv", "run", "--frozen", "ruff", "format", "--check", *backend], cwd)

    if prettier or eslint:
        bin_dir = root / FRONTEND / "node_modules" / ".bin"
        if not (bin_dir / "prettier").exists():
            print("pre-commit: frontend node_modules missing, skipping prettier/eslint", file=sys.stderr)
        else:
            cwd = root / FRONTEND
            if prettier:
                ok &= _run([str(bin_dir / "prettier"), "--check", *prettier], cwd)
            if eslint:
                ok &= _run([str(bin_dir / "eslint"), "--max-warnings=0", *eslint], cwd)

    if not ok:
        print("pre-commit: lint failed; fix the files above (or PLUMMY_SKIP_LINT=1 once)", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
