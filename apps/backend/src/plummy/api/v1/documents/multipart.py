"""Streaming ``multipart/form-data`` reader for uploads (SECURITY_MODEL.md §9).

FastAPI's ``UploadFile`` spools the whole body to a temporary file before the handler runs, so neither the
access check nor the size limit could happen first. This reader parses the request stream incrementally:

1. small text fields, which must come before the file (the handler authorizes with them);
2. exactly one file part, handed out as an async iterator while it arrives;
3. the rest of the body, which may contain nothing but the closing boundary.

Nothing is buffered beyond one network chunk; the declared ``Content-Length`` is checked up front.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Final

from python_multipart.exceptions import MultipartParseError
from python_multipart.multipart import MultipartParser, parse_options_header
from starlette.requests import Request

from plummy.application.errors import InvalidInputError, PayloadTooLargeError

MAX_FIELD_BYTES: Final = 256
MAX_FIELDS: Final = 4
# Boundaries, part headers and text fields around the file.
ENVELOPE_BYTES: Final = 16 * 1024


def _invalid(code: str, message: str) -> InvalidInputError:
    return InvalidInputError("file", code, message)


@dataclass
class _Part:
    name: str
    filename: str | None


@dataclass
class _Events:
    """Collected by the parser callbacks during one ``write``."""

    items: list[tuple[str, object]] = field(default_factory=list)
    header_field: bytearray = field(default_factory=bytearray)
    header_value: bytearray = field(default_factory=bytearray)
    headers: dict[str, bytes] = field(default_factory=dict)


class MultipartUpload:
    def __init__(self, request: Request, *, max_file_bytes: int) -> None:
        content_type, params = parse_options_header(request.headers.get("content-type", ""))
        boundary = params.get(b"boundary")
        if content_type != b"multipart/form-data" or not boundary:
            raise _invalid("multipart_required", "Send the file as multipart/form-data.")
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > max_file_bytes + ENVELOPE_BYTES:
            raise PayloadTooLargeError(f"The file is larger than {max_file_bytes} bytes.")
        self._request = request
        self._events = _Events()
        self._parser = MultipartParser(
            boundary,
            {
                "on_part_begin": self._on_part_begin,
                "on_header_field": lambda data, start, end: self._events.header_field.extend(data[start:end]),
                "on_header_value": lambda data, start, end: self._events.header_value.extend(data[start:end]),
                "on_header_end": self._on_header_end,
                "on_headers_finished": self._on_headers_finished,
                "on_part_data": lambda data, start, end: self._events.items.append(
                    ("data", bytes(data[start:end]))
                ),
                "on_part_end": lambda: self._events.items.append(("part_end", None)),
                "on_end": lambda: self._events.items.append(("end", None)),
            },
        )
        self._stream = self._parse()
        self._finished = False

    # --- parser callbacks -----------------------------------------------------------------------------

    def _on_part_begin(self) -> None:
        self._events.headers = {}

    def _on_header_end(self) -> None:
        events = self._events
        events.headers[bytes(events.header_field).decode("latin-1").lower()] = bytes(events.header_value)
        events.header_field.clear()
        events.header_value.clear()

    def _on_headers_finished(self) -> None:
        disposition, params = parse_options_header(self._events.headers.get("content-disposition", b""))
        name = params.get(b"name")
        if disposition != b"form-data" or name is None:
            raise _invalid("malformed_multipart", "Every part needs a form-data name.")
        filename = params.get(b"filename")
        self._events.items.append(
            (
                "part",
                _Part(
                    name.decode("utf-8", "replace"), filename.decode("utf-8", "replace") if filename else None
                ),
            )
        )

    # --- reading --------------------------------------------------------------------------------------

    async def _parse(self) -> AsyncIterator[tuple[str, object]]:
        async for chunk in self._request.stream():
            if not chunk:
                continue
            try:
                self._parser.write(chunk)
            except MultipartParseError:
                raise _invalid("malformed_multipart", "The multipart body is malformed.") from None
            items, self._events.items = self._events.items, []
            for item in items:
                yield item
        if not self._finished:
            raise _invalid("malformed_multipart", "The multipart body ended early.")

    async def fields_before_file(self) -> tuple[dict[str, str], str | None]:
        """Text fields up to the file part, and the client's file name (display only)."""
        fields: dict[str, str] = {}
        current: _Part | None = None
        value = bytearray()
        async for kind, payload in self._stream:
            if isinstance(payload, _Part):
                if payload.filename is not None:
                    return fields, payload.filename
                if len(fields) >= MAX_FIELDS or payload.name in fields:
                    raise _invalid("malformed_multipart", "Unexpected form fields.")
                current, value = payload, bytearray()
            elif isinstance(payload, bytes):
                if current is None:
                    raise _invalid("malformed_multipart", "The multipart body is malformed.")
                value += payload
                if len(value) > MAX_FIELD_BYTES:
                    raise _invalid("field_too_large", "A form field is too large.")
            elif kind == "part_end" and current is not None:
                fields[current.name] = value.decode("utf-8", "replace")
                current = None
            elif kind == "end":
                self._finished = True
                break
        raise _invalid("file_required", "The form has no file.")

    async def file_content(self) -> AsyncIterator[bytes]:
        """The file bytes; afterwards the body must end without another part."""
        async for kind, payload in self._stream:
            if isinstance(payload, bytes):
                yield payload
            elif kind == "part_end":
                break
        async for kind, _ in self._stream:
            if kind == "part":
                raise _invalid("unexpected_part", "Send one file per request, after the other fields.")
            if kind == "end":
                self._finished = True
        if not self._finished:
            raise _invalid("malformed_multipart", "The multipart body ended early.")
