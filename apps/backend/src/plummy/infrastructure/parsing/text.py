"""Parser for plain-text formats: TXT, Markdown, CSV (ARCHITECTURE.md §8.3).

Pure Python and bounded by the limits, so it runs inside the worker process. Stages 3-4 already verified the
file is UTF-8 without NUL bytes.
"""

from __future__ import annotations

import asyncio
import csv
import io
import re
from pathlib import Path
from typing import Final

from plummy.application.ingestion.parsing import (
    BlockKind,
    DocumentBlock,
    NormalizedDocument,
    ParseError,
    ParseLimits,
)
from plummy.domain.file_types import DocumentFormat

PARSER_NAME: Final = "plummy-text/1"
MAX_HEADING_LENGTH: Final = 200
MAX_CELL_CHARS: Final = 2000
HEADING: Final = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
FENCE: Final = re.compile(r"^(```|~~~)")
LIST_ITEM: Final = re.compile(r"^\s*(?:[-*+]|\d{1,9}[.)])\s+")


def _too_long() -> ParseError:
    return ParseError("text_limit_exceeded", "The document has more text than allowed.")


class PlainTextParser:
    @property
    def name(self) -> str:
        return PARSER_NAME

    def supported_formats(self) -> frozenset[DocumentFormat]:
        return frozenset({DocumentFormat.TXT, DocumentFormat.MARKDOWN, DocumentFormat.CSV})

    async def parse(self, path: Path, file_format: DocumentFormat, limits: ParseLimits) -> NormalizedDocument:
        return await asyncio.to_thread(self._parse, path, file_format, limits)

    def _parse(self, path: Path, file_format: DocumentFormat, limits: ParseLimits) -> NormalizedDocument:
        if path.stat().st_size > limits.max_text_chars * 4:  # at most 4 UTF-8 bytes per character
            raise _too_long()
        text = path.read_text(encoding="utf-8-sig")
        if len(text) > limits.max_text_chars:
            raise _too_long()
        if file_format is DocumentFormat.CSV:
            blocks = _csv_rows(text, limits)
        elif file_format is DocumentFormat.MARKDOWN:
            blocks = _markdown(text)
        else:
            blocks = _paragraphs(text)
        if not blocks:
            raise ParseError("no_text", "The document contains no text.")
        return NormalizedDocument(blocks=tuple(blocks))


def _paragraphs(text: str) -> list[DocumentBlock]:
    blocks = []
    for paragraph in re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n")):
        cleaned = paragraph.strip()
        if cleaned:
            blocks.append(DocumentBlock(BlockKind.PARAGRAPH, cleaned))
    return blocks


def _markdown(text: str) -> list[DocumentBlock]:
    blocks: list[DocumentBlock] = []
    sections: list[tuple[int, str]] = []
    buffer: list[str] = []
    fence: str | None = None

    def path() -> tuple[str, ...]:
        return tuple(title for _, title in sections)

    def flush(kind: BlockKind = BlockKind.PARAGRAPH) -> None:
        content = "\n".join(buffer).strip("\n")
        buffer.clear()
        if content.strip():
            if kind is BlockKind.PARAGRAPH and LIST_ITEM.match(content):
                kind = BlockKind.LIST_ITEM
            blocks.append(DocumentBlock(kind, content.strip(), section_path=path()))

    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if fence is not None:
            buffer.append(line)
            if line.strip().startswith(fence):
                fence = None
                flush(BlockKind.CODE)
            continue
        if match := FENCE.match(line.strip()):
            flush()
            fence = match.group(1)
            buffer.append(line)
            continue
        if match := HEADING.match(line):
            flush()
            level, title = len(match.group(1)), match.group(2).strip()[:MAX_HEADING_LENGTH]
            while sections and sections[-1][0] >= level:
                sections.pop()
            if title:
                sections.append((level, title))
                blocks.append(DocumentBlock(BlockKind.HEADING, title, section_path=path(), level=level))
            continue
        if not line.strip():
            flush()
            continue
        buffer.append(line)
    flush(BlockKind.CODE if fence is not None else BlockKind.PARAGRAPH)
    return blocks


def _csv_rows(text: str, limits: ParseLimits) -> list[DocumentBlock]:
    sample = text[:16_384]
    try:
        dialect: type[csv.Dialect] | csv.Dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    # a stream, not splitlines(): a quoted cell may contain line breaks
    reader = csv.reader(io.StringIO(text, newline=""), dialect)
    blocks: list[DocumentBlock] = []
    header: list[str] | None = None
    try:
        for row in reader:
            cells = [cell.strip()[:MAX_CELL_CHARS] for cell in row]
            if not any(cells):
                continue
            if header is None:
                header = [cell or f"column {index}" for index, cell in enumerate(cells, start=1)]
                continue
            if len(blocks) >= limits.max_rows:
                raise ParseError("row_limit_exceeded", "The table has more rows than allowed.")
            pairs = [
                f"{header[index] if index < len(header) else f'column {index + 1}'}: {value}"
                for index, value in enumerate(cells)
                if value
            ]
            # the line where the record starts would need a lookahead; the line where it ends is exact
            blocks.append(DocumentBlock(BlockKind.ROW, "; ".join(pairs), row=reader.line_num))
    except csv.Error:
        raise ParseError("parse_failed", "The CSV file is malformed.") from None
    if header is not None and not blocks:
        # A header without data rows is still searchable text.
        blocks.append(DocumentBlock(BlockKind.ROW, "; ".join(header), row=1))
    return blocks
