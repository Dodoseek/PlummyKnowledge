"""Application version.

The single source of truth is the ``VERSION`` file at the repository root (ADR-009). The backend root is
``apps/backend`` in the repository and ``/app`` in Docker images, so in both cases the file is two levels
above it (``/VERSION`` inside containers).
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[3]
VERSION_FILE = BACKEND_ROOT.parent.parent / "VERSION"
UNKNOWN_VERSION = "0.0.0+unknown"

_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:-dev\.\d+)?$")


def read_version(path: Path = VERSION_FILE) -> str:
    """Return the version from ``path`` or ``UNKNOWN_VERSION`` if it is missing or malformed."""
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        return UNKNOWN_VERSION
    return value if _VERSION_RE.fullmatch(value) else UNKNOWN_VERSION


@lru_cache(maxsize=1)
def get_version() -> str:
    return read_version()
