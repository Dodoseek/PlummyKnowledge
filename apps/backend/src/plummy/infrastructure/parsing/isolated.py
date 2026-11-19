"""Isolated parser: runs ``plummy.infrastructure.parsing.child`` in a separate process (SECURITY_MODEL.md §9).

- a hard timeout (the process group is killed) → ``parse_timeout``;
- rlimits: address space, CPU time, open files, file size, no core dumps;
- a minimal environment: no database URL, keys or provider secrets reach the parser, Hugging Face is offline;
- stdout carries one JSON object with a size cap; stderr is drained and discarded (it may echo document text);
- the child gets the file as ``document.<ext>`` (a symlink in a private directory): openpyxl and Docling pick
  the reader by extension, storage keys have none, and the extension comes from the format confirmed by
  content (stages 3-4), never from the uploaded file name.

The worker-ingestion container adds the outer boundary: no network, separate queue (DEPLOYMENT.md).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import resource
import shutil
import signal
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Final

from plummy.application.ingestion.parsing import NormalizedDocument, ParseError, ParseLimits
from plummy.domain.file_types import DocumentFormat

CHILD_MODULE: Final = "plummy.infrastructure.parsing.child"
PASSTHROUGH_ENV: Final = ("PATH", "LANG", "LC_ALL", "DOCLING_ARTIFACTS_PATH", "HF_HOME", "PYTHONPATH")
MAX_OPEN_FILES: Final = 256
MAX_WRITTEN_FILE_BYTES: Final = 512 * 1024 * 1024
PIPE_CHUNK: Final = 64 * 1024


def child_environment(scratch: Path, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {name: os.environ[name] for name in PASSTHROUGH_ENV if name in os.environ}
    env.update(
        {
            "HOME": str(scratch),
            "TMPDIR": str(scratch),
            "HF_HUB_OFFLINE": "1",
            "TRANSFORMERS_OFFLINE": "1",
            "HF_HUB_DISABLE_TELEMETRY": "1",
            "OMP_NUM_THREADS": "2",
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONUNBUFFERED": "1",
        }
    )
    env.update(extra or {})
    return env


def _limit_resources(limits: ParseLimits) -> None:  # pragma: no cover - runs in the child before exec
    os.setsid()
    resource.setrlimit(resource.RLIMIT_AS, (limits.memory_bytes, limits.memory_bytes))
    cpu = limits.timeout_seconds + 5
    resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 5))
    resource.setrlimit(resource.RLIMIT_NOFILE, (MAX_OPEN_FILES, MAX_OPEN_FILES))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_WRITTEN_FILE_BYTES, MAX_WRITTEN_FILE_BYTES))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))


class IsolatedParser:
    """Implements ``ParserProvider`` for the formats handled by the child process."""

    def __init__(
        self,
        name: str,
        formats: frozenset[DocumentFormat],
        *,
        scratch_dir: Path,
        python: str = sys.executable,
        extra_env: Mapping[str, str] | None = None,
    ) -> None:
        self._name, self._formats = name, formats
        self._scratch, self._python, self._extra_env = scratch_dir, python, dict(extra_env or {})

    @property
    def name(self) -> str:
        return self._name

    def supported_formats(self) -> frozenset[DocumentFormat]:
        return self._formats

    async def parse(self, path: Path, file_format: DocumentFormat, limits: ParseLimits) -> NormalizedDocument:
        if file_format not in self._formats:
            raise ParseError("unsupported_media_type", "No parser is available for this format.")
        source = await asyncio.to_thread(self._link_input, path, file_format)
        try:
            return await self._run(source, file_format, limits)
        finally:
            await asyncio.to_thread(shutil.rmtree, source.parent, ignore_errors=True)

    def _link_input(self, path: Path, file_format: DocumentFormat) -> Path:
        self._scratch.mkdir(mode=0o700, parents=True, exist_ok=True)
        workdir = Path(tempfile.mkdtemp(prefix="parse-", dir=self._scratch))
        source = workdir / f"document.{file_format.value}"
        source.symlink_to(path.resolve())
        return source

    async def _run(
        self, source: Path, file_format: DocumentFormat, limits: ParseLimits
    ) -> NormalizedDocument:
        request = json.dumps(
            {
                "path": str(source),
                "format": file_format.value,
                "max_pages": limits.max_pages,
                "max_sheets": limits.max_sheets,
                "max_rows": limits.max_rows,
                "max_text_chars": limits.max_text_chars,
            }
        ).encode()
        process = await asyncio.create_subprocess_exec(
            self._python,
            "-s",  # no user site-packages
            "-P",  # neither the cwd nor the script directory on sys.path; PYTHONPATH still applies
            "-m",
            CHILD_MODULE,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=child_environment(self._scratch, self._extra_env),
            cwd=self._scratch,
            preexec_fn=lambda: _limit_resources(limits),
            close_fds=True,
        )
        try:
            stdout = await asyncio.wait_for(
                self._communicate(process, request, limits.max_output_bytes), timeout=limits.timeout_seconds
            )
        except TimeoutError:
            raise ParseError("parse_timeout", "Parsing took longer than allowed.") from None
        finally:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                await process.wait()
        return self._result(process.returncode, stdout)

    async def _communicate(self, process: asyncio.subprocess.Process, request: bytes, cap: int) -> bytes:
        if process.stdin is None or process.stdout is None or process.stderr is None:
            raise RuntimeError("parser pipes are missing")
        process.stdin.write(request)
        await process.stdin.drain()
        process.stdin.close()
        drain = asyncio.create_task(self._discard(process.stderr))
        output = bytearray()
        while chunk := await process.stdout.read(PIPE_CHUNK):
            output += chunk
            if len(output) > cap:
                raise ParseError("text_limit_exceeded", "The document has more text than allowed.")
        await process.wait()
        await drain
        return bytes(output)

    @staticmethod
    async def _discard(stream: asyncio.StreamReader) -> None:
        while await stream.read(PIPE_CHUNK):
            pass

    @staticmethod
    def _result(returncode: int | None, stdout: bytes) -> NormalizedDocument:
        try:
            decoded = json.loads(stdout) if stdout else {}
        except ValueError:
            decoded = {}
        payload: dict[str, Any] = decoded if isinstance(decoded, dict) else {}
        if payload.get("ok") is True:
            try:
                return NormalizedDocument.from_json(payload["document"])
            except (KeyError, TypeError, ValueError):
                raise ParseError("parse_failed", "The parser returned an invalid result.") from None
        if payload.get("ok") is False and isinstance(payload.get("code"), str):
            raise ParseError(
                payload["code"], str(payload.get("message") or "The document could not be parsed.")
            )
        if returncode is not None and returncode < 0:
            # Killed by a signal: the memory or CPU limit, or the kernel's OOM killer.
            raise ParseError("parse_resource_limit", "Parsing exceeded the allowed memory or CPU time.")
        raise ParseError("parse_failed", "The document could not be parsed.")
