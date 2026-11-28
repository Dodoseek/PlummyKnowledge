"""A hostile corpus against the type and safety checks of the pipeline (SECURITY_MODEL §9, Phase 12.4).

XML declarations (XXE, entity expansion), nested archives and polyglots. The same kinds of files against
the real Docling parser, without these checks in front of it: ``tests/ingestion/test_docling_parser.py``.
"""

from __future__ import annotations

import io
import secrets
import zipfile
from pathlib import Path

import pytest

from plummy.application.ingestion import inspection
from plummy.application.ingestion.inspection import MAX_PROLOG_BYTES, inspect_file
from plummy.domain.file_types import ContainerKind, DocumentFormat, sniff_container
from tests.unit.test_file_inspection import DOCX_TYPES, XLSX_TYPES, rejected

BODY = b'<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body/></w:document>'
DECLARATION = b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'
XXE = b'<!DOCTYPE w:document [<!ENTITY x SYSTEM "file:///etc/passwd">]>'
LAUGHS = (
    b'<!DOCTYPE lolz [<!ENTITY a "lol"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
    b'<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">]>'
)


def package(path: Path, entries: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return path


def docx(path: Path, document: bytes, extra: dict[str, bytes] | None = None) -> Path:
    entries = {"[Content_Types].xml": DOCX_TYPES.encode(), "word/document.xml": document, **(extra or {})}
    return package(path, entries)


# --- XML declarations -------------------------------------------------------------------------------------


def test_a_clean_document_passes(tmp_path: Path) -> None:
    assert inspect_file(docx(tmp_path / "ok", DECLARATION + BODY), "ok.docx") is DocumentFormat.DOCX


@pytest.mark.parametrize(
    "document",
    [
        DECLARATION + XXE + BODY,
        DECLARATION + LAUGHS + BODY,
        XXE + BODY,  # no XML declaration
        DECLARATION + b"<!-- a comment -->\n<?pi data?>\n" + XXE + BODY,  # behind comments and PIs
        b"\xef\xbb\xbf" + DECLARATION + XXE + BODY,  # UTF-8 BOM
        "﻿<?xml version='1.0' encoding='UTF-16'?><!DOCTYPE d [<!ENTITY x 'y'>]><d/>".encode("utf-16-le"),
        DECLARATION + b"<!ENTITY x SYSTEM 'file:///etc/passwd'>" + BODY,  # a bare declaration
        DECLARATION + b"<!-- " + b"x" * MAX_PROLOG_BYTES + b" -->" + BODY,  # padding to hide one further on
        DECLARATION + b"<!-- never closed",
    ],
)
def test_xml_declarations_are_rejected_in_the_main_part(tmp_path: Path, document: bytes) -> None:
    assert rejected(docx(tmp_path / "xxe", document)) == "unsafe_content"


@pytest.mark.parametrize(
    "part",
    [
        "word/styles.xml",
        "word/_rels/document.xml.rels",
        "_rels/.rels",
        "customXml/item1.xml",
        "word/drawings/vmlDrawing1.vml",
        "WORD/FOOTER1.XML",
    ],
)
def test_xml_declarations_are_rejected_in_any_part(tmp_path: Path, part: str) -> None:
    path = docx(tmp_path / "part", DECLARATION + BODY, {part: DECLARATION + XXE + b"<r/>"})
    assert rejected(path) == "unsafe_content"


def test_the_content_types_part_is_checked_too(tmp_path: Path) -> None:
    types = DECLARATION.decode() + XXE.decode() + DOCX_TYPES.removeprefix('<?xml version="1.0"?>')
    path = package(tmp_path / "types", {"[Content_Types].xml": types.encode(), "word/document.xml": BODY})
    assert rejected(path) == "unsafe_content"


def test_xlsx_parts_are_checked_like_docx(tmp_path: Path) -> None:
    entries = {"[Content_Types].xml": XLSX_TYPES.encode(), "xl/workbook.xml": BODY}
    path = package(tmp_path / "x", {**entries, "xl/worksheets/sheet1.xml": XXE + b"<s/>"})
    assert rejected(path) == "unsafe_content"


def test_doctype_text_inside_the_body_is_just_text(tmp_path: Path) -> None:
    # After the root element a "<!DOCTYPE" is not a declaration (a parser rejects it as malformed XML).
    document = DECLARATION + BODY.replace(b"<w:body/>", b"<w:body><w:t>&lt;!DOCTYPE x&gt;</w:t></w:body>")
    assert inspect_file(docx(tmp_path / "text", document), "t.docx") is DocumentFormat.DOCX


def test_only_the_prolog_of_each_part_is_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    paragraphs = b"".join(
        b"<w:p><w:t>" + secrets.token_hex(16).encode() + b"</w:t></w:p>" for _ in range(30_000)
    )
    big = DECLARATION + BODY.replace(b"<w:body/>", b"<w:body>" + paragraphs + b"</w:body>")
    path = docx(tmp_path / "big", big)
    reads: list[int] = []
    original = zipfile.ZipExtFile.read

    def counting(self: zipfile.ZipExtFile, n: int = -1) -> bytes:
        data = original(self, n)
        reads.append(len(data))
        return data

    monkeypatch.setattr(zipfile.ZipExtFile, "read", counting)
    assert inspect_file(path, "big.docx") is DocumentFormat.DOCX
    assert len(big) > 1_000_000
    assert sum(reads) <= 2 * MAX_PROLOG_BYTES + inspection.MAX_CONTENT_TYPES_BYTES


# --- nested archives ----------------------------------------------------------------------------------------


def zip_bytes(**entries: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)
    return buffer.getvalue()


def test_a_nested_bomb_is_never_unpacked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """An embedded archive counts at its stored size and is not opened: the outer limits bound the work."""
    inner = zip_bytes(**{f"layer{i}.bin": b"\x00" * (4 * 1024 * 1024) for i in range(16)})  # 64 MiB inside
    assert len(inner) < 1_000_000
    path = docx(tmp_path / "nested", DECLARATION + BODY, {"word/embeddings/package.zip": inner})
    opened: list[str] = []
    original = zipfile.ZipFile.open

    def recording(
        self: zipfile.ZipFile, name: str | zipfile.ZipInfo, *args: object, **kwargs: object
    ) -> object:
        opened.append(name if isinstance(name, str) else name.filename)
        return original(self, name, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(zipfile.ZipFile, "open", recording)
    assert inspect_file(path, "nested.docx") is DocumentFormat.DOCX
    assert "word/embeddings/package.zip" not in opened


def test_an_archive_of_documents_is_not_a_document(tmp_path: Path) -> None:
    inner = docx(tmp_path / "inner", DECLARATION + BODY).read_bytes()
    path = tmp_path / "outer.docx"
    path.write_bytes(zip_bytes(**{"report.docx": inner, "other.docx": inner}))
    assert rejected(path, "outer.docx") == "unsupported_media_type"


def test_a_nested_archive_beyond_the_ratio_is_rejected(tmp_path: Path) -> None:
    # Stored deflated in the outer archive, an embedding is held to the same ratio as any entry.
    zeros = {"word/embeddings/zeros.bin": b"\x00" * (8 * 1024 * 1024)}
    assert rejected(docx(tmp_path / "ratio", DECLARATION + BODY, zeros)) == "archive_limits_exceeded"


# --- polyglots ----------------------------------------------------------------------------------------------


def test_a_pdf_with_an_archive_appended_is_a_pdf(tmp_path: Path) -> None:
    path = tmp_path / "poly.docx"
    path.write_bytes(
        b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n" + zip_bytes(**{"[Content_Types].xml": DOCX_TYPES.encode()})
    )
    assert sniff_container(path.read_bytes()[:8192], complete=False) is ContainerKind.PDF
    # The bytes decide, not the name; the PDF parser never opens the appended archive.
    assert inspect_file(path, "poly.docx") is DocumentFormat.PDF


def test_an_archive_with_a_pdf_header_inside_is_an_archive(tmp_path: Path) -> None:
    path = docx(tmp_path / "poly", DECLARATION + BODY, {"fake.pdf": b"%PDF-1.4 fake"})
    assert sniff_container(path.read_bytes()[:8192], complete=False) is ContainerKind.ZIP
    assert inspect_file(path, "poly.pdf") is DocumentFormat.DOCX


def test_html_and_script_in_a_text_file_stay_text(tmp_path: Path) -> None:
    path = tmp_path / "page.md"
    path.write_bytes(b"<html><script>alert(1)</script><img src=x onerror=alert(2)></html>\n# Title\n")
    assert inspect_file(path, "page.md") is DocumentFormat.MARKDOWN  # shown as data (viewer, chat)


def test_a_foreign_header_before_a_pdf_header_is_a_pdf_never_text(tmp_path: Path) -> None:
    for head in (b"GIF89a\x01\x00\x01\x00\x80\x00\x00", b"MZ\x90\x00\x03\x00\x00\x00"):
        path = tmp_path / "poly"
        path.write_bytes(head + b"\x00\x00%PDF-1.4\n" + b"x" * 100)
        # The PDF header may follow leading bytes; the PDF parser then decides (a broken one fails).
        assert inspect_file(path, "poly.txt") is DocumentFormat.PDF
