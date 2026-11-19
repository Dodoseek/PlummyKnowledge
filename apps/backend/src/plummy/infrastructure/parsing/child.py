"""Parser child process: ``python -m plummy.infrastructure.parsing.child`` (started by ``IsolatedParser``).

Reads one JSON request on stdin, writes one JSON object on stdout. Runs under rlimits with a minimal
environment and, in production, in the ``worker-ingestion`` container without network access.

- PDF and DOCX: Docling (layout and table structure models from the image, OCR off: scanned PDFs are out of
  the MVP, PRODUCT_SPEC §2);
- XLSX: openpyxl in read-only mode, so every row keeps its sheet and row number for citations.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any, Final

from plummy.application.ingestion.parsing import (
    BlockKind,
    DocumentBlock,
    NormalizedDocument,
    ParseError,
)
from plummy.domain.file_types import DocumentFormat

MAX_REQUEST_BYTES: Final = 64 * 1024
MAX_CELL_CHARS: Final = 2000
MAX_HEADING_CHARS: Final = 200


class _TextBudget:
    def __init__(self, limit: int) -> None:
        self.left = limit

    def take(self, text: str) -> str:
        self.left -= len(text)
        if self.left < 0:
            raise ParseError("text_limit_exceeded", "The document has more text than allowed.")
        return text


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, dt.datetime | dt.date | dt.time):
        return value.isoformat()
    return " ".join(str(value).split())[:MAX_CELL_CHARS]


def parse_xlsx(path: Path, request: dict[str, Any]) -> NormalizedDocument:
    from openpyxl import load_workbook

    budget = _TextBudget(int(request["max_text_chars"]))
    max_rows, max_sheets = int(request["max_rows"]), int(request["max_sheets"])
    try:
        workbook = load_workbook(path, read_only=True, data_only=True, keep_links=False)
    except Exception:
        raise ParseError("parse_failed", "The spreadsheet could not be read.") from None
    blocks: list[DocumentBlock] = []
    try:
        if len(workbook.sheetnames) > max_sheets:
            raise ParseError("sheet_limit_exceeded", "The workbook has more sheets than allowed.")
        for sheet in workbook.worksheets:
            name = str(sheet.title)[:MAX_HEADING_CHARS]
            header: list[str] | None = None
            for number, values in enumerate(sheet.iter_rows(values_only=True), start=1):
                cells = [_cell(value) for value in values]
                if not any(cells):
                    continue
                if header is None:
                    header = [cell or f"column {index}" for index, cell in enumerate(cells, start=1)]
                    blocks.append(DocumentBlock(BlockKind.HEADING, budget.take(name), sheet=name, level=1))
                    continue
                if len(blocks) >= max_rows:
                    raise ParseError("row_limit_exceeded", "The workbook has more rows than allowed.")
                pairs = [
                    f"{header[index] if index < len(header) else f'column {index + 1}'}: {value}"
                    for index, value in enumerate(cells)
                    if value
                ]
                text = budget.take("; ".join(pairs))
                blocks.append(
                    DocumentBlock(BlockKind.ROW, text, section_path=(name,), sheet=name, row=number)
                )
    finally:
        workbook.close()
    return NormalizedDocument(blocks=tuple(blocks), metadata={"sheets": str(len(workbook.sheetnames))})


def _pdf_page_count(path: Path) -> int:
    import pypdfium2 as pdfium

    try:
        document = pdfium.PdfDocument(str(path))
    except pdfium.PdfiumError as exc:
        if "password" in str(exc).lower():
            raise ParseError("encrypted_file", "Password-protected files are not supported.") from None
        raise ParseError("parse_failed", "The PDF could not be read.") from None
    try:
        return len(document)
    finally:
        document.close()


def parse_with_docling(
    path: Path, file_format: DocumentFormat, request: dict[str, Any]
) -> NormalizedDocument:
    from docling.datamodel.base_models import ConversionStatus, InputFormat
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.document_converter import DocumentConverter, PdfFormatOption

    max_pages = int(request["max_pages"])
    page_count = None
    if file_format is DocumentFormat.PDF:
        page_count = _pdf_page_count(path)
        if page_count > max_pages:
            raise ParseError("page_limit_exceeded", "The document has more pages than allowed.")
    options = PdfPipelineOptions(
        artifacts_path=os.environ.get("DOCLING_ARTIFACTS_PATH") or None,
        do_ocr=False,
        do_table_structure=True,
        enable_remote_services=False,
    )
    converter = DocumentConverter(
        allowed_formats=[InputFormat.PDF, InputFormat.DOCX],
        format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)},
    )
    try:
        result = converter.convert(path, max_num_pages=max_pages, raises_on_error=False)
    except Exception:
        raise ParseError("parse_failed", "The document could not be parsed.") from None
    if result.status not in (ConversionStatus.SUCCESS, ConversionStatus.PARTIAL_SUCCESS):
        raise ParseError("parse_failed", "The document could not be parsed.")
    document = result.document
    blocks = _docling_blocks(document, _TextBudget(int(request["max_text_chars"])))
    return NormalizedDocument(blocks=tuple(blocks), page_count=page_count or document.num_pages() or None)


def _docling_blocks(document: Any, budget: _TextBudget) -> list[DocumentBlock]:
    from docling_core.types.doc import (
        CodeItem,
        DocItemLabel,
        ListItem,
        SectionHeaderItem,
        TableItem,
        TextItem,
        TitleItem,
    )

    skipped = {DocItemLabel.PAGE_HEADER, DocItemLabel.PAGE_FOOTER}
    sections: list[tuple[int, str]] = []
    blocks: list[DocumentBlock] = []
    for item, _ in document.iterate_items():
        provenance = getattr(item, "prov", None) or []
        page = provenance[0].page_no if provenance else None
        if isinstance(item, TitleItem | SectionHeaderItem):
            level = 0 if isinstance(item, TitleItem) else int(getattr(item, "level", 1) or 1)
            title = " ".join(item.text.split())[:MAX_HEADING_CHARS]
            if not title:
                continue
            while sections and sections[-1][0] >= level:
                sections.pop()
            sections.append((level, title))
            path = tuple(name for _, name in sections)
            blocks.append(
                DocumentBlock(
                    BlockKind.HEADING, budget.take(title), page=page, section_path=path, level=level
                )
            )
            continue
        path = tuple(name for _, name in sections)
        if isinstance(item, TableItem):
            text = item.export_to_markdown(doc=document).strip()
            kind = BlockKind.TABLE
        elif isinstance(item, CodeItem):
            text, kind = item.text.strip(), BlockKind.CODE
        elif isinstance(item, ListItem):
            text, kind = item.text.strip(), BlockKind.LIST_ITEM
        elif isinstance(item, TextItem) and item.label not in skipped:
            text, kind = item.text.strip(), BlockKind.PARAGRAPH
        else:
            continue  # pictures, page headers and footers
        if text:
            blocks.append(DocumentBlock(kind, budget.take(text), page=page, section_path=path))
    return blocks


def parse(request: dict[str, Any]) -> NormalizedDocument:
    path, file_format = Path(str(request["path"])), DocumentFormat(request["format"])
    if file_format is DocumentFormat.XLSX:
        document = parse_xlsx(path, request)
    elif file_format in (DocumentFormat.PDF, DocumentFormat.DOCX):
        document = parse_with_docling(path, file_format, request)
    else:
        raise ParseError("unsupported_media_type", "No parser is available for this format.")
    if not any(block.text.strip() for block in document.blocks):
        raise ParseError("no_text", "The document contains no extractable text.")
    return document


def main() -> int:
    raw = sys.stdin.buffer.read(MAX_REQUEST_BYTES + 1)
    try:
        if len(raw) > MAX_REQUEST_BYTES:
            raise ParseError("parse_failed", "Invalid parser request.")
        document = parse(json.loads(raw))
        reply: dict[str, Any] = {"ok": True, "document": document.to_json()}
    except ParseError as exc:
        reply = {"ok": False, "code": exc.code, "message": exc.message}
    except MemoryError:
        reply = {
            "ok": False,
            "code": "parse_resource_limit",
            "message": "Parsing exceeded the allowed memory.",
        }
    except Exception:
        reply = {"ok": False, "code": "parse_failed", "message": "The document could not be parsed."}
    sys.stdout.write(json.dumps(reply, ensure_ascii=False))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
