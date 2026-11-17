from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

from plummy.core.logging import JsonFormatter, request_id_var
from plummy.core.version import UNKNOWN_VERSION, VERSION_FILE, read_version


def _record(message: str, **extra: object) -> logging.LogRecord:
    record = logging.LogRecord("plummy.test", logging.INFO, __file__, 1, message, None, None)
    record.__dict__.update(extra)
    return record


def test_json_formatter_emits_one_json_object_with_extra_fields() -> None:
    output = JsonFormatter().format(_record("hello\nworld", check="redis"))
    assert "\n" not in output
    payload = json.loads(output)
    assert payload["message"] == "hello\nworld"
    assert payload["level"] == "INFO"
    assert payload["logger"] == "plummy.test"
    assert payload["check"] == "redis"
    assert payload["timestamp"].endswith("+00:00")
    assert "request_id" not in payload


def test_json_formatter_includes_request_id_from_context() -> None:
    token = request_id_var.set("req-123")
    try:
        payload = json.loads(JsonFormatter().format(_record("x")))
    finally:
        request_id_var.reset(token)
    assert payload["request_id"] == "req-123"


def test_json_formatter_includes_exception_type() -> None:
    try:
        raise ValueError("boom")
    except ValueError:
        record = _record("failed")
        record.exc_info = sys.exc_info()
    payload = json.loads(JsonFormatter().format(record))
    assert payload["exc_type"] == "ValueError"
    assert "Traceback" in payload["exc_info"]


def test_repository_version_file_is_valid() -> None:
    assert VERSION_FILE.exists(), "VERSION must be two levels above the backend root (ADR-009)"
    assert read_version() != UNKNOWN_VERSION


@pytest.mark.parametrize("content", ["0.1.0-dev.7\n", "1.2.3"])
def test_read_version_accepts_semver_subset(tmp_path: Path, content: str) -> None:
    path = tmp_path / "VERSION"
    path.write_text(content, encoding="utf-8")
    assert read_version(path) == content.strip()


@pytest.mark.parametrize("content", ["", "v1.2.3", "1.2", "1.2.3-beta", "<script>"])
def test_read_version_rejects_malformed_values(tmp_path: Path, content: str) -> None:
    path = tmp_path / "VERSION"
    path.write_text(content, encoding="utf-8")
    assert read_version(path) == UNKNOWN_VERSION


def test_read_version_handles_missing_file(tmp_path: Path) -> None:
    assert read_version(tmp_path / "missing") == UNKNOWN_VERSION
