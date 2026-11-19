"""``ParserProvider`` port and the parser-independent ``NormalizedDocument`` (ARCHITECTURE.md §8.3).

Stages 6-7 of the pipeline (ADR-008). Documents are untrusted input: parsers run with explicit limits, and
their output is plain text with its position (page, section, sheet, rows); nothing in it is executed or
interpreted later.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Final, Protocol

from plummy.domain.file_types import DocumentFormat

NORMALIZED_VERSION: Final = 1


class BlockKind(StrEnum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    TABLE = "table"
    CODE = "code"
    ROW = "row"  # one spreadsheet / CSV row, rendered as "column: value" pairs


@dataclass(frozen=True, slots=True)
class DocumentBlock:
    kind: BlockKind
    text: str
    page: int | None = None
    section_path: tuple[str, ...] = ()
    sheet: str | None = None
    row: int | None = None  # 1-based row number in the sheet or file
    level: int | None = None  # heading level

    def to_json(self) -> dict[str, Any]:
        data: dict[str, Any] = {"kind": self.kind.value, "text": self.text}
        for name in ("page", "sheet", "row", "level"):
            if (value := getattr(self, name)) is not None:
                data[name] = value
        if self.section_path:
            data["section_path"] = list(self.section_path)
        return data

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> DocumentBlock:
        return cls(
            kind=BlockKind(data["kind"]),
            text=str(data["text"]),
            page=data.get("page"),
            section_path=tuple(str(item) for item in data.get("section_path", ())),
            sheet=data.get("sheet"),
            row=data.get("row"),
            level=data.get("level"),
        )


@dataclass(frozen=True, slots=True)
class NormalizedDocument:
    blocks: tuple[DocumentBlock, ...]
    page_count: int | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)

    @property
    def text_length(self) -> int:
        return sum(len(block.text) for block in self.blocks)

    def to_json(self) -> dict[str, Any]:
        return {
            "version": NORMALIZED_VERSION,
            "page_count": self.page_count,
            "metadata": dict(self.metadata),
            "blocks": [block.to_json() for block in self.blocks],
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> NormalizedDocument:
        if data.get("version") != NORMALIZED_VERSION:
            raise ValueError("unsupported normalized document version")
        return cls(
            blocks=tuple(DocumentBlock.from_json(block) for block in data["blocks"]),
            page_count=data.get("page_count"),
            metadata={str(key): str(value) for key, value in dict(data.get("metadata") or {}).items()},
        )


@dataclass(frozen=True, slots=True)
class ParseLimits:
    """Defaults are [Proposed] values of SECURITY_MODEL.md §9."""

    timeout_seconds: int = 300
    max_pages: int = 2000
    max_sheets: int = 100
    max_rows: int = 200_000
    max_text_chars: int = 20_000_000
    # Address-space cap of the parser process. PyTorch reserves far more virtual memory than it uses (3 GiB
    # fails on a one-page PDF), so this only stops runaway allocations; resident memory is bounded by the
    # worker-ingestion container's cgroup limit (an OOM kill ends as ``parse_resource_limit``).
    memory_bytes: int = 12 * 1024 * 1024 * 1024
    max_output_bytes: int = 128 * 1024 * 1024


class ParseError(Exception):
    """A permanent parsing failure (the job is not retried). ``message`` never contains document text."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class ParserProvider(Protocol):
    @property
    def name(self) -> str:
        """Recorded in ``document_versions.parser`` (e.g. ``docling/2.150.1``)."""
        ...

    def supported_formats(self) -> frozenset[DocumentFormat]: ...

    async def parse(self, path: Path, file_format: DocumentFormat, limits: ParseLimits) -> NormalizedDocument:
        """Raises :class:`ParseError`; ``OSError`` and other exceptions are treated as transient."""
        ...


class ParserRegistry:
    """Chooses the parser by the format confirmed in stages 3-4."""

    def __init__(self, parsers: list[ParserProvider]) -> None:
        self._by_format: dict[DocumentFormat, ParserProvider] = {}
        for parser in parsers:
            for file_format in parser.supported_formats():
                self._by_format.setdefault(file_format, parser)

    def for_format(self, file_format: DocumentFormat) -> ParserProvider:
        parser = self._by_format.get(file_format)
        if parser is None:
            raise ParseError("unsupported_media_type", "No parser is available for this format.")
        return parser
