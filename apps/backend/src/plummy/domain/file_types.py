"""Document formats of the MVP and detection by content (SECURITY_MODEL.md §9, PRODUCT_SPEC §6).

The type is decided by the bytes, never by the extension or the client's ``Content-Type``. The extension only
chooses between the plain-text formats (TXT, CSV, Markdown), whose content is verified to be text.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Final

PDF_MAGIC: Final = b"%PDF-"
PDF_HEADER_WINDOW: Final = 1024  # the PDF header may follow some leading bytes
ZIP_MAGIC: Final = b"PK\x03\x04"
UTF8_BOM: Final = b"\xef\xbb\xbf"
SNIFF_BYTES: Final = 8192


class DocumentFormat(StrEnum):
    PDF = "pdf"
    DOCX = "docx"
    XLSX = "xlsx"
    TXT = "txt"
    CSV = "csv"
    MARKDOWN = "md"

    @property
    def mime_type(self) -> str:
        return _MIME_TYPES[self]

    @property
    def is_text(self) -> bool:
        return self in TEXT_FORMATS


_MIME_TYPES: Final = {
    DocumentFormat.PDF: "application/pdf",
    DocumentFormat.DOCX: "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    DocumentFormat.XLSX: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    DocumentFormat.TXT: "text/plain",
    DocumentFormat.CSV: "text/csv",
    DocumentFormat.MARKDOWN: "text/markdown",
}
TEXT_FORMATS: Final = frozenset({DocumentFormat.TXT, DocumentFormat.CSV, DocumentFormat.MARKDOWN})
_TEXT_EXTENSIONS: Final = {
    "csv": DocumentFormat.CSV,
    "md": DocumentFormat.MARKDOWN,
    "markdown": DocumentFormat.MARKDOWN,
}


class ContainerKind(StrEnum):
    """What the first bytes say; ZIP containers are told apart only by their contents (worker)."""

    PDF = "pdf"
    ZIP = "zip"
    TEXT = "text"


def text_format_for(filename: str) -> DocumentFormat:
    _, dot, extension = filename.rpartition(".")
    return _TEXT_EXTENSIONS.get(extension.lower(), DocumentFormat.TXT) if dot else DocumentFormat.TXT


def looks_like_text(data: bytes, *, complete: bool) -> bool:
    """UTF-8 without NUL bytes. With ``complete=False`` a multi-byte character cut at the end is accepted."""
    if b"\x00" in data:
        return False
    body = data.removeprefix(UTF8_BOM)
    try:
        body.decode("utf-8")
    except UnicodeDecodeError as exc:
        # Only a truncated character at the very end of a partial read is acceptable.
        return not complete and exc.reason == "unexpected end of data" and exc.start >= len(body) - 3
    return True


def sniff_container(head: bytes, *, complete: bool) -> ContainerKind | None:
    """Classify the first bytes of a file; ``None`` means a format outside the MVP (or empty)."""
    if not head:
        return None
    if head.startswith(ZIP_MAGIC):
        return ContainerKind.ZIP
    if PDF_MAGIC in head[:PDF_HEADER_WINDOW]:
        return ContainerKind.PDF
    if looks_like_text(head, complete=complete):
        return ContainerKind.TEXT
    return None


def provisional_format(kind: ContainerKind, filename: str) -> DocumentFormat:
    """Best guess at intake; the worker confirms it from the whole file (ZIP contents, full text check)."""
    if kind is ContainerKind.PDF:
        return DocumentFormat.PDF
    if kind is ContainerKind.TEXT:
        return text_format_for(filename)
    return DocumentFormat.XLSX if filename.lower().endswith(".xlsx") else DocumentFormat.DOCX
