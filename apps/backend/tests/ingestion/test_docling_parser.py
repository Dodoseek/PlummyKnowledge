"""The real parser child (Docling, openpyxl) behind IsolatedParser, as the worker-ingestion image runs it.

Files have no extension, as in storage. Runs only where the "ingestion" extra is installed (the
worker-ingestion image, CI job "Ingestion"); the models come from ``DOCLING_ARTIFACTS_PATH`` and Hugging Face
is offline, so nothing is downloaded.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from plummy.application.ingestion.parsing import BlockKind, NormalizedDocument, ParseError, ParseLimits
from plummy.domain.file_types import DocumentFormat
from plummy.infrastructure.parsing.isolated import IsolatedParser

pytest.importorskip("docling", reason="the ingestion extra is not installed")
pytestmark = pytest.mark.ingestion

FORMATS = frozenset({DocumentFormat.PDF, DocumentFormat.DOCX, DocumentFormat.XLSX})
LIMITS = ParseLimits(timeout_seconds=240)


def pdf_bytes(pages: list[list[str]], *, encrypt: bool = False) -> bytes:
    """A minimal text PDF written by hand (one Helvetica line per entry)."""
    font_id = 3 + 2 * len(pages)
    objects: list[bytes] = []
    kids = []
    for index, lines in enumerate(pages):
        page_id, content_id = 3 + 2 * index, 4 + 2 * index
        kids.append(f"{page_id} 0 R")
        ops = ["BT", "/F1 14 Tf", "72 720 Td", "18 TL", *(f"({line}) Tj T*" for line in lines), "ET"]
        stream = "\n".join(ops).encode()
        objects.append(
            f"{page_id} 0 obj << /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            f"/Resources << /Font << /F1 {font_id} 0 R >> >> /Contents {content_id} 0 R >> endobj\n".encode()
        )
        objects.append(
            f"{content_id} 0 obj << /Length {len(stream)} >> stream\n".encode()
            + stream
            + b"\nendstream endobj\n"
        )
    head = [
        b"1 0 obj << /Type /Catalog /Pages 2 0 R >> endobj\n",
        f"2 0 obj << /Type /Pages /Kids [{' '.join(kids)}] /Count {len(pages)} >> endobj\n".encode(),
    ]
    tail = [f"{font_id} 0 obj << /Type /Font /Subtype /Type1 /BaseFont /Helvetica >> endobj\n".encode()]
    body, offsets = b"%PDF-1.4\n", []
    for obj in head + objects + tail:
        offsets.append(len(body))
        body += obj
    xref = len(body)
    body += f"xref\n0 {len(offsets) + 1}\n0000000000 65535 f \n".encode()
    body += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    body += f"trailer << /Size {len(offsets) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return body


@pytest.fixture
def parser(tmp_path: Path) -> IsolatedParser:
    assert os.environ.get("HF_HUB_OFFLINE") == "1"
    return IsolatedParser("docling/test", FORMATS, scratch_dir=tmp_path / "scratch")


async def parse(
    parser: IsolatedParser, path: Path, file_format: DocumentFormat, limits: ParseLimits = LIMITS
) -> NormalizedDocument:
    return await parser.parse(path, file_format, limits)


async def test_pdf_text_with_pages(parser: IsolatedParser, tmp_path: Path) -> None:
    path = tmp_path / "policy"
    path.write_bytes(
        pdf_bytes(
            [
                ["Vacation policy", "Employees get 28 days of paid leave."],
                ["Transfers", "Leave can be moved."],
            ]
        )
    )
    document = await parse(parser, path, DocumentFormat.PDF)
    assert document.page_count == 2
    text = {block.page: block.text for block in document.blocks}
    joined = " ".join(block.text for block in document.blocks)
    assert "28 days of paid leave" in joined
    assert "Leave can be moved" in joined
    assert set(text) == {1, 2}


async def test_pdf_page_limit_is_checked_before_conversion(parser: IsolatedParser, tmp_path: Path) -> None:
    path = tmp_path / "long"
    path.write_bytes(pdf_bytes([[f"Page {n}"] for n in range(3)]))
    with pytest.raises(ParseError) as error:
        await parse(parser, path, DocumentFormat.PDF, ParseLimits(timeout_seconds=240, max_pages=2))
    assert error.value.code == "page_limit_exceeded"


async def test_broken_pdf(parser: IsolatedParser, tmp_path: Path) -> None:
    path = tmp_path / "broken"
    path.write_bytes(b"%PDF-1.4\n" + os.urandom(2048))
    with pytest.raises(ParseError) as error:
        await parse(parser, path, DocumentFormat.PDF)
    assert error.value.code == "parse_failed"


async def test_docx_headings_sections_and_table(parser: IsolatedParser, tmp_path: Path) -> None:
    from docx import Document

    source = Document()
    source.add_heading("Handbook", 1)
    source.add_paragraph("Welcome to the company.")
    source.add_heading("Leave", 2)
    source.add_paragraph("Leave is 28 days.")
    table = source.add_table(rows=2, cols=2)
    for (row, column), value in {(0, 0): "Type", (0, 1): "Days", (1, 0): "Annual", (1, 1): "28"}.items():
        table.cell(row, column).text = value
    path = tmp_path / "handbook"
    source.save(str(path))

    document = await parse(parser, path, DocumentFormat.DOCX)
    headings = [
        (block.text, block.section_path) for block in document.blocks if block.kind is BlockKind.HEADING
    ]
    assert headings == [("Handbook", ("Handbook",)), ("Leave", ("Handbook", "Leave"))]
    paragraph = next(block for block in document.blocks if block.text == "Leave is 28 days.")
    assert paragraph.section_path == ("Handbook", "Leave")
    [table_block] = [block for block in document.blocks if block.kind is BlockKind.TABLE]
    assert "Annual" in table_block.text
    assert "28" in table_block.text


async def test_xlsx_rows_keep_sheet_and_row_numbers(parser: IsolatedParser, tmp_path: Path) -> None:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    assert sheet is not None
    sheet.title = "Budget"
    sheet.append(["Item", "Cost"])
    sheet.append(["Laptops", 1200.0])
    sheet.append([None, None])
    sheet.append(["Desks", 300])
    workbook.create_sheet("Empty")
    path = tmp_path / "budget"
    workbook.save(path)

    document = await parse(parser, path, DocumentFormat.XLSX)
    assert [(b.kind, b.text, b.sheet, b.row) for b in document.blocks] == [
        (BlockKind.HEADING, "Budget", "Budget", None),
        (BlockKind.ROW, "Item: Laptops; Cost: 1200", "Budget", 2),
        (BlockKind.ROW, "Item: Desks; Cost: 300", "Budget", 4),
    ]

    limited = ParseLimits(timeout_seconds=240, max_sheets=1)
    with pytest.raises(ParseError) as error:
        await parse(parser, path, DocumentFormat.XLSX, limited)
    assert error.value.code == "sheet_limit_exceeded"


async def test_timeout_and_memory_limits_apply_to_docling(parser: IsolatedParser, tmp_path: Path) -> None:
    path = tmp_path / "policy"
    path.write_bytes(pdf_bytes([["Some text"]]))
    with pytest.raises(ParseError) as error:
        await parse(parser, path, DocumentFormat.PDF, ParseLimits(timeout_seconds=1))
    assert error.value.code == "parse_timeout"
    with pytest.raises(ParseError) as error:
        await parse(
            parser, path, DocumentFormat.PDF, ParseLimits(timeout_seconds=240, memory_bytes=64 * 1024**2)
        )
    assert error.value.code in {"parse_resource_limit", "parse_failed"}
