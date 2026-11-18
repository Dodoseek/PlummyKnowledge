"""Bootstrap token file on the server (ADR-006): ``<BOOTSTRAP_DIR>/setup-token``, mode 0600, dir 0700."""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

TOKEN_FILE_NAME = "setup-token"  # noqa: S105 - a file name


class FileBootstrapTokenStore:
    def __init__(self, directory: Path) -> None:
        self._directory = directory
        self._path = directory / TOKEN_FILE_NAME

    @property
    def location(self) -> str:
        return str(self._path)

    def read(self) -> str | None:
        try:
            return self._path.read_text(encoding="ascii").strip() or None
        except (FileNotFoundError, UnicodeDecodeError):
            return None

    def write(self, token: str) -> None:
        self._directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        with contextlib.suppress(PermissionError):
            os.chmod(self._directory, 0o700)
        temporary = self._path.with_name(f".{TOKEN_FILE_NAME}.tmp")
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
        # O_EXCL + 0600: the file is never readable by other users, not even briefly.
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="ascii") as handle:
            handle.write(token + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self._path)

    def remove(self) -> bool:
        try:
            self._path.unlink()
        except FileNotFoundError:
            return False
        return True
