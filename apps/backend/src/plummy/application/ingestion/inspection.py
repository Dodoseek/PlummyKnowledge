"""Stages 3-4 of the pipeline: type and safety of the whole file (ADR-008 §3, SECURITY_MODEL.md §9).

Runs in the worker on the quarantined file, for every source. Nothing is extracted to disk: ZIP
containers (DOCX, XLSX) are checked from their central directory, ``[Content_Types].xml`` is read with a
cap, and of every other XML part only the prolog. Nested archives are never opened by the pipeline, so the
outer limits bound the work.

XML parts must not declare a DTD: OPC (ECMA-376 Part 2) forbids document type declarations in package XML,
so no legitimate document has one, and refusing them removes XXE and entity expansion ("billion laughs")
before any parser sees the file (SECURITY_MODEL.md §9).
"""

from __future__ import annotations

import codecs
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Final

from plummy.domain.file_types import (
    PDF_HEADER_WINDOW,
    PDF_MAGIC,
    SNIFF_BYTES,
    UTF8_BOM,
    ZIP_MAGIC,
    DocumentFormat,
    text_format_for,
)

READ_CHUNK: Final = 1024 * 1024
CONTENT_TYPES: Final = "[Content_Types].xml"
MAX_CONTENT_TYPES_BYTES: Final = 1024 * 1024
# The prolog of an OOXML part is an XML declaration; a longer one is padding meant to hide a declaration.
MAX_PROLOG_BYTES: Final = 64 * 1024
XML_SUFFIXES: Final = (".xml", ".rels", ".vml")
DOCX_MAIN: Final = "wordprocessingml.document.main+xml"
XLSX_MAIN: Final = "spreadsheetml.sheet.main+xml"


@dataclass(frozen=True, slots=True)
class ArchiveLimits:
    """Defaults are [Proposed] values of SECURITY_MODEL.md §9."""

    max_entries: int = 10_000
    max_unpacked_bytes: int = 512 * 1024 * 1024
    max_ratio: int = 100  # uncompressed / compressed, per entry
    ratio_floor_bytes: int = 1024 * 1024  # small entries compress well legitimately (XML)


class FileRejectedError(Exception):
    """A permanent rejection: the job is not retried. ``message`` never contains file content."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def unsupported(message: str = "The file is not a supported format.") -> FileRejectedError:
    return FileRejectedError("unsupported_media_type", message)


def inspect_file(path: Path, display_name: str, limits: ArchiveLimits | None = None) -> DocumentFormat:
    """The format of the file at ``path`` or :class:`FileRejectedError`."""
    with path.open("rb") as handle:
        head = handle.read(SNIFF_BYTES)
    if not head:
        raise FileRejectedError("empty_file", "The file is empty.")
    if head.startswith(ZIP_MAGIC):
        return _inspect_zip(path, limits or ArchiveLimits())
    if PDF_MAGIC in head[:PDF_HEADER_WINDOW]:
        return DocumentFormat.PDF
    _check_text(path)
    return text_format_for(display_name)


def _check_text(path: Path) -> None:
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    first = True
    with path.open("rb") as handle:
        while chunk := handle.read(READ_CHUNK):
            if first:
                chunk, first = chunk.removeprefix(UTF8_BOM), False
            if b"\x00" in chunk:
                raise unsupported()
            try:
                decoder.decode(chunk)
            except UnicodeDecodeError:
                raise FileRejectedError("invalid_text_encoding", "Text files must be UTF-8.") from None
    try:
        decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        raise FileRejectedError("invalid_text_encoding", "Text files must be UTF-8.") from None


def _inspect_zip(path: Path, limits: ArchiveLimits) -> DocumentFormat:
    try:
        with zipfile.ZipFile(path) as archive:
            entries = archive.infolist()
            _check_entries(entries, limits)
            names = {entry.filename.lower() for entry in entries}
            if any(PurePosixPath(name).name == "vbaproject.bin" for name in names):
                raise _macro()
            content_types = _read_content_types(archive)
            for entry in entries:
                if entry.filename.lower().endswith(XML_SUFFIXES) and _declares_dtd(archive, entry):
                    raise _unsafe()
    except (zipfile.BadZipFile, zipfile.LargeZipFile, NotImplementedError, EOFError):
        raise FileRejectedError("malformed_file", "The file is damaged or not a valid document.") from None
    if "macroenabled" in content_types.lower():
        raise _macro()
    if DOCX_MAIN in content_types:
        return DocumentFormat.DOCX
    if XLSX_MAIN in content_types:
        return DocumentFormat.XLSX
    raise unsupported("Only DOCX and XLSX are supported among Office files.")


def _check_entries(entries: list[zipfile.ZipInfo], limits: ArchiveLimits) -> None:
    if len(entries) > limits.max_entries:
        raise _too_large()
    total = 0
    for entry in entries:
        if entry.flag_bits & 0x1:
            raise FileRejectedError("encrypted_file", "Password-protected files are not supported.")
        parts = PurePosixPath(entry.filename.replace("\\", "/")).parts
        if entry.filename.startswith(("/", "\\")) or ".." in parts:
            raise FileRejectedError("malformed_file", "The file is damaged or not a valid document.")
        total += entry.file_size
        if total > limits.max_unpacked_bytes:
            raise _too_large()
        if entry.file_size > limits.ratio_floor_bytes and (
            entry.compress_size == 0 or entry.file_size / entry.compress_size > limits.max_ratio
        ):
            raise _too_large()


def _read_content_types(archive: zipfile.ZipFile) -> str:
    try:
        info = archive.getinfo(CONTENT_TYPES)
    except KeyError:
        raise unsupported() from None
    if info.file_size > MAX_CONTENT_TYPES_BYTES:
        raise _too_large()
    with archive.open(info) as handle:
        data = handle.read(MAX_CONTENT_TYPES_BYTES + 1)
    if len(data) > MAX_CONTENT_TYPES_BYTES:
        raise _too_large()
    return data.decode("utf-8", errors="replace")


def _declares_dtd(archive: zipfile.ZipFile, entry: zipfile.ZipInfo) -> bool:
    """Whether the prolog of an XML part has a markup declaration (``<!DOCTYPE``, ``<!ENTITY``, ...).

    Only the prolog is read: a DTD can only stand before the root element. A prolog that does not end within
    :data:`MAX_PROLOG_BYTES` counts as declaring one.
    """
    with archive.open(entry) as handle:
        head = handle.read(MAX_PROLOG_BYTES)
    if head.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = head.decode("utf-16", errors="ignore")
    else:
        text = head.removeprefix(UTF8_BOM).decode("utf-8", errors="ignore")
    position = 0
    while True:
        while position < len(text) and text[position] in " \t\r\n":
            position += 1
        if position >= len(text):
            return len(head) >= MAX_PROLOG_BYTES  # an empty or whitespace part is the parser's problem
        if text.startswith("<?", position):
            end = text.find("?>", position + 2)
            terminator = 2
        elif text.startswith("<!--", position):
            end = text.find("-->", position + 4)
            terminator = 3
        else:
            return text.startswith("<!", position)
        if end < 0:
            return True
        position = end + terminator


def _unsafe() -> FileRejectedError:
    return FileRejectedError(
        "unsafe_content", "XML declarations (DTD, entities) are not accepted in documents."
    )


def _macro() -> FileRejectedError:
    return FileRejectedError("macro_enabled", "Files with macros are not accepted.")


def _too_large() -> FileRejectedError:
    return FileRejectedError("archive_limits_exceeded", "The document unpacks beyond the allowed limits.")
