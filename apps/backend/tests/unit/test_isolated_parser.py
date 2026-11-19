"""IsolatedParser: the process boundary (timeout, rlimits, environment, output cap, error mapping).

The child here is a small script standing in for ``plummy.infrastructure.parsing.child``: the boundary is
the same for any program. The real child with Docling is covered by tests/ingestion.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from plummy.application.ingestion.parsing import (
    BlockKind,
    DocumentBlock,
    NormalizedDocument,
    ParseError,
    ParseLimits,
    ParserRegistry,
)
from plummy.domain.file_types import DocumentFormat
from plummy.infrastructure.parsing import child
from plummy.infrastructure.parsing.isolated import IsolatedParser, child_environment
from plummy.infrastructure.parsing.text import PlainTextParser

FORMATS = frozenset({DocumentFormat.PDF})
OK = {
    "ok": True,
    "document": NormalizedDocument((DocumentBlock(BlockKind.PARAGRAPH, "x", page=1),), 1).to_json(),
}


def fake_child(tmp_path: Path, body: str) -> str:
    """An executable that ignores ``-s -P -m module`` and runs ``body`` as Python."""
    script = tmp_path / "fake-python"
    script.write_text(f"#!{sys.executable}\nimport json, os, sys, time\n{body}\n")
    script.chmod(0o700)
    return str(script)


async def run(tmp_path: Path, body: str, limits: ParseLimits | None = None) -> NormalizedDocument:
    parser = IsolatedParser(
        "fake/1", FORMATS, scratch_dir=tmp_path / "scratch", python=fake_child(tmp_path, body)
    )
    document = tmp_path / "stored-object"  # storage keys have no extension
    document.write_bytes(b"%PDF-1.4")
    return await parser.parse(document, DocumentFormat.PDF, limits or ParseLimits(timeout_seconds=20))


async def code_of(tmp_path: Path, body: str, limits: ParseLimits | None = None) -> str:
    with pytest.raises(ParseError) as error:
        await run(tmp_path, body, limits)
    return error.value.code


async def test_request_and_successful_reply(tmp_path: Path) -> None:
    body = f"""
request = json.loads(sys.stdin.read())
assert request["format"] == "pdf", request
source = request["path"]
assert source.endswith("/document.pdf") and os.path.islink(source), source
assert os.path.realpath(source).endswith("/stored-object"), source
assert open(source, "rb").read() == b"%PDF-1.4"
assert set(request) == {{"path", "format", "max_pages", "max_sheets", "max_rows", "max_text_chars"}}
sys.stdout.write({json.dumps(json.dumps(OK))})
"""
    document = await run(tmp_path, body)
    assert [(b.kind, b.text, b.page) for b in document.blocks] == [(BlockKind.PARAGRAPH, "x", 1)]
    assert list((tmp_path / "scratch").iterdir()) == []  # the per-call directory is removed
    assert document.page_count == 1


async def test_child_error_reply_is_passed_through(tmp_path: Path) -> None:
    body = 'print(json.dumps({"ok": False, "code": "page_limit_exceeded", "message": "Too many pages."}))'
    with pytest.raises(ParseError) as error:
        await run(tmp_path, body)
    assert (error.value.code, error.value.message) == ("page_limit_exceeded", "Too many pages.")


async def test_timeout_kills_the_whole_process_group(tmp_path: Path) -> None:
    marker = tmp_path / "grandchild-alive"
    body = f"""
if os.fork() == 0:
    time.sleep(3)
    open({str(marker)!r}, "w").close()
    os._exit(0)
time.sleep(60)
"""
    assert await code_of(tmp_path, body, ParseLimits(timeout_seconds=1)) == "parse_timeout"
    import asyncio

    await asyncio.sleep(3.5)
    assert not marker.exists()  # the grandchild was killed with its group


async def test_memory_limit(tmp_path: Path) -> None:
    body = "data = bytearray(512 * 1024 * 1024)\nprint(json.dumps({'ok': True, 'document': {}}))"
    code = await code_of(tmp_path, body, ParseLimits(timeout_seconds=20, memory_bytes=256 * 1024 * 1024))
    assert code in {"parse_failed", "parse_resource_limit"}  # MemoryError traceback or killed


async def test_killed_by_signal_is_a_resource_limit(tmp_path: Path) -> None:
    assert (
        await code_of(tmp_path, "import signal\nos.kill(os.getpid(), signal.SIGKILL)")
        == "parse_resource_limit"
    )


async def test_output_cap(tmp_path: Path) -> None:
    body = "sys.stdout.write('x' * 300_000)"
    limits = ParseLimits(timeout_seconds=20, max_output_bytes=100_000)
    assert await code_of(tmp_path, body, limits) == "text_limit_exceeded"


@pytest.mark.parametrize(
    "body",
    [
        "sys.exit(3)",
        "print('not json')",
        "print(json.dumps({'ok': True, 'document': {'version': 99}}))",
        "print(json.dumps({'ok': True}))",
        "print(json.dumps([1, 2]))",
        "raise SystemExit('crash with ' + 'document text on stderr')",
    ],
)
async def test_broken_replies_are_parse_failures(tmp_path: Path, body: str) -> None:
    assert await code_of(tmp_path, body) == "parse_failed"


async def test_child_sees_only_a_minimal_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:secret@db/plummy")
    monkeypatch.setenv("SECRETS_KEYRING_FILE", "/var/lib/plummy/secrets/keyring.json")
    monkeypatch.setenv("OPENAI_API_KEY", "not-a-real-key")
    body = f"""
import resource
env = dict(os.environ)
names = ("RLIMIT_AS", "RLIMIT_NOFILE", "RLIMIT_CORE")
limits = {{name: resource.getrlimit(getattr(resource, name))[0] for name in names}}
reply = json.loads({json.dumps(json.dumps(OK))})
reply["document"]["metadata"] = {{"env": json.dumps(env), "limits": json.dumps(limits), "cwd": os.getcwd(),
    "sid": str(os.getsid(0) == os.getpid())}}
sys.stdout.write(json.dumps(reply))
"""
    document = await run(tmp_path, body, ParseLimits(timeout_seconds=20, memory_bytes=2 * 1024**3))
    env = json.loads(document.metadata["env"])
    assert not {"DATABASE_URL", "SECRETS_KEYRING_FILE", "OPENAI_API_KEY", "REDIS_URL"} & set(env)
    assert env["HF_HUB_OFFLINE"] == "1"
    assert env["HOME"] == env["TMPDIR"] == str(tmp_path / "scratch")
    assert json.loads(document.metadata["limits"]) == {
        "RLIMIT_AS": 2 * 1024**3,
        "RLIMIT_NOFILE": 256,
        "RLIMIT_CORE": 0,
    }
    assert document.metadata["cwd"] == str(tmp_path / "scratch")
    assert document.metadata["sid"] == "True"
    assert (tmp_path / "scratch").stat().st_mode & 0o777 == 0o700


def test_child_environment_extra_values_win(tmp_path: Path) -> None:
    env = child_environment(tmp_path, {"OMP_NUM_THREADS": "1"})
    assert env["OMP_NUM_THREADS"] == "1"


async def test_unsupported_format_is_rejected_before_starting(tmp_path: Path) -> None:
    parser = IsolatedParser("fake/1", FORMATS, scratch_dir=tmp_path, python="/nonexistent")
    with pytest.raises(ParseError) as error:
        await parser.parse(tmp_path / "a.docx", DocumentFormat.DOCX, ParseLimits())
    assert error.value.code == "unsupported_media_type"


def test_registry_picks_the_first_parser_for_a_format(tmp_path: Path) -> None:
    text, office = (
        PlainTextParser(),
        IsolatedParser("o", FORMATS | {DocumentFormat.TXT}, scratch_dir=tmp_path),
    )
    registry = ParserRegistry([text, office])
    assert registry.for_format(DocumentFormat.TXT) is text
    assert registry.for_format(DocumentFormat.PDF) is office
    with pytest.raises(ParseError) as error:
        registry.for_format(DocumentFormat.XLSX)
    assert error.value.code == "unsupported_media_type"


# --- the child entry point without the parser stack ------------------------------------------------------


def child_reply(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], raw: bytes
) -> dict[str, object]:
    class Stdin:
        buffer = __import__("io").BytesIO(raw)

    monkeypatch.setattr(sys, "stdin", Stdin)
    assert child.main() == 0
    reply: dict[str, object] = json.loads(capsys.readouterr().out)
    return reply


def test_child_rejects_oversized_and_malformed_requests(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert child_reply(monkeypatch, capsys, b"{" * (child.MAX_REQUEST_BYTES + 1))["code"] == "parse_failed"
    assert child_reply(monkeypatch, capsys, b"not json")["code"] == "parse_failed"
    request = {
        "path": "/x",
        "format": "txt",
        "max_pages": 1,
        "max_sheets": 1,
        "max_rows": 1,
        "max_text_chars": 1,
    }
    assert child_reply(monkeypatch, capsys, json.dumps(request).encode())["code"] == "unsupported_media_type"


def test_normalized_document_round_trip_and_version() -> None:
    block = DocumentBlock(BlockKind.ROW, "a: 1", section_path=("Sheet",), sheet="Sheet", row=2)
    document = NormalizedDocument((block,), page_count=None, metadata={"sheets": "1"})
    data = document.to_json()
    assert data["blocks"] == [
        {"kind": "row", "text": "a: 1", "sheet": "Sheet", "row": 2, "section_path": ["Sheet"]}
    ]
    assert NormalizedDocument.from_json(data) == document
    assert document.text_length == 4
    with pytest.raises(ValueError, match="version"):
        NormalizedDocument.from_json({**data, "version": 2})
