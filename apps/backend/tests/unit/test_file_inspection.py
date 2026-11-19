"""Type by content and file safety checks (SECURITY_MODEL.md §9, ADR-008 stages 3-4)."""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from plummy.application.ingestion.inspection import (
    READ_CHUNK,
    ArchiveLimits,
    FileRejectedError,
    inspect_file,
)
from plummy.domain.file_types import (
    ContainerKind,
    DocumentFormat,
    provisional_format,
    sniff_container,
    text_format_for,
)

DOCX_TYPES = (
    '<?xml version="1.0"?><Types><Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>'
)
XLSX_TYPES = (
    '<?xml version="1.0"?><Types><Override PartName="/xl/workbook.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/></Types>'
)
DOCM_TYPES = DOCX_TYPES.replace(
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
    "application/vnd.ms-word.document.macroEnabled.main+xml",
)


def office(path: Path, content_types: str | None, **entries: bytes) -> Path:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        if content_types is not None:
            archive.writestr("[Content_Types].xml", content_types)
        for name, data in entries.items():
            archive.writestr(name.replace("__", "/"), data)
    return path


def rejected(path: Path, name: str = "file", limits: ArchiveLimits | None = None) -> str:
    with pytest.raises(FileRejectedError) as exc_info:
        inspect_file(path, name, limits)
    return exc_info.value.code


# --- first bytes (intake) -----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("head", "kind"),
    [
        (b"%PDF-1.7\n%\xe2\xe3", ContainerKind.PDF),
        (b"\n\n%PDF-1.4", ContainerKind.PDF),
        (b"PK\x03\x04\x14\x00", ContainerKind.ZIP),
        (b"plain text, \xd0\xbf\xd1\x80\xd0\xb8\xd0\xb2\xd0\xb5\xd1\x82", ContainerKind.TEXT),
        (b"\xef\xbb\xbfa,b\n1,2\n", ContainerKind.TEXT),
        (b"\x89PNG\r\n\x1a\n", None),
        (b"MZ\x90\x00", None),  # executable
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", None),  # legacy .doc/.xls
        (b"text with a \x00 byte", None),
        (b"\xcf\xf0\xe8\xe2\xe5\xf2", None),  # cp1251, not UTF-8
        (b"", None),
    ],
)
def test_sniff_container(head: bytes, kind: ContainerKind | None) -> None:
    assert sniff_container(head, complete=True) is kind


def test_a_character_cut_by_the_sniff_window_is_still_text() -> None:
    cut = "привет".encode()[:-1]
    assert sniff_container(cut, complete=False) is ContainerKind.TEXT
    assert sniff_container(cut, complete=True) is None


def test_extension_only_chooses_among_text_formats() -> None:
    assert text_format_for("table.CSV") is DocumentFormat.CSV
    assert text_format_for("notes.md") is DocumentFormat.MARKDOWN
    assert text_format_for("readme") is DocumentFormat.TXT
    assert text_format_for("evil.exe") is DocumentFormat.TXT
    assert provisional_format(ContainerKind.PDF, "looks.docx") is DocumentFormat.PDF
    assert provisional_format(ContainerKind.ZIP, "sheet.xlsx") is DocumentFormat.XLSX


# --- whole file (worker) ------------------------------------------------------------------------------------


def test_office_documents_are_told_apart_by_their_content(tmp_path: Path) -> None:
    docx = office(tmp_path / "a.bin", DOCX_TYPES, word__document_xml=b"<w:document/>")
    xlsx = office(tmp_path / "b.bin", XLSX_TYPES, xl__workbook_xml=b"<workbook/>")
    # the name does not matter
    assert inspect_file(docx, "report.xlsx") is DocumentFormat.DOCX
    assert inspect_file(xlsx, "report.docx") is DocumentFormat.XLSX


def test_pdf_and_text(tmp_path: Path) -> None:
    pdf = tmp_path / "x"
    pdf.write_bytes(b"%PDF-1.7\n...")
    assert inspect_file(pdf, "scan.txt") is DocumentFormat.PDF
    text = tmp_path / "t"
    text.write_bytes("﻿name,value\nжжж,1\n".encode())
    assert inspect_file(text, "data.csv") is DocumentFormat.CSV
    assert inspect_file(text, "data.md") is DocumentFormat.MARKDOWN


def test_text_is_checked_to_the_end(tmp_path: Path) -> None:
    path = tmp_path / "t"
    # a multi-byte character across the read boundary is fine
    path.write_bytes(b"a" * (READ_CHUNK - 1) + "ж".encode() + b"tail")
    assert inspect_file(path, "big.txt") is DocumentFormat.TXT
    path.write_bytes(b"a" * (READ_CHUNK + 10) + b"\x00")
    assert rejected(path) == "unsupported_media_type"
    path.write_bytes(b"a" * (READ_CHUNK + 10) + b"\xcf\xf0")
    assert rejected(path) == "invalid_text_encoding"
    path.write_bytes(b"ends inside a character " + "ж".encode()[:1])
    assert rejected(path) == "invalid_text_encoding"


def test_empty_file(tmp_path: Path) -> None:
    path = tmp_path / "empty"
    path.write_bytes(b"")
    assert rejected(path) == "empty_file"


@pytest.mark.parametrize(
    "build",
    [
        lambda p: office(p, DOCM_TYPES),
        lambda p: office(p, DOCX_TYPES, **{"word/vbaProject.bin": b"macro"}),
        lambda p: office(p, XLSX_TYPES, **{"xl/VBAPROJECT.BIN": b"macro"}),
    ],
)
def test_macro_enabled_files_are_rejected(tmp_path: Path, build: object) -> None:
    path = build(tmp_path / "m")  # type: ignore[operator]
    assert rejected(path) == "macro_enabled"


def test_other_zip_files_are_unsupported(tmp_path: Path) -> None:
    assert rejected(office(tmp_path / "a", None, notes_txt=b"x")) == "unsupported_media_type"
    pptx = (
        '<Types><Override ContentType="application/'
        'vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/></Types>'
    )
    assert rejected(office(tmp_path / "b", pptx)) == "unsupported_media_type"


def test_decompression_bomb_is_rejected_without_unpacking(tmp_path: Path) -> None:
    path = office(tmp_path / "bomb", DOCX_TYPES, word__document_xml=b"\x00" * (8 * 1024 * 1024))
    assert path.stat().st_size < 100_000
    assert rejected(path) == "archive_limits_exceeded"


def test_archive_limits(tmp_path: Path) -> None:
    many = office(tmp_path / "many", DOCX_TYPES, **{f"word__p{i}_xml": b"x" for i in range(20)})
    assert rejected(many, limits=ArchiveLimits(max_entries=10)) == "archive_limits_exceeded"
    assert inspect_file(many, "f", ArchiveLimits(max_entries=30)) is DocumentFormat.DOCX
    big = office(tmp_path / "big", DOCX_TYPES, word__document_xml=bytes(range(256)) * 400)
    assert rejected(big, limits=ArchiveLimits(max_unpacked_bytes=50_000)) == "archive_limits_exceeded"


def test_encrypted_and_traversal_entries(tmp_path: Path) -> None:
    path = office(tmp_path / "enc", DOCX_TYPES, word__document_xml=b"ciphertext")
    # zipfile cannot write encrypted entries: set the "encrypted" flag of the last central directory entry
    data = bytearray(path.read_bytes())
    entry = data.rfind(b"PK\x01\x02")
    data[entry + 8] |= 0x1
    path.write_bytes(bytes(data))
    assert rejected(path) == "encrypted_file"
    for name in ("../../etc/cron.d/x", "/abs/path.xml", "word\\..\\..\\x.xml"):
        traversal = office(tmp_path / "t", DOCX_TYPES, **{"x": b""})
        with zipfile.ZipFile(traversal, "a") as archive:
            archive.writestr(name, b"x")
        assert rejected(traversal) == "malformed_file", name


def test_truncated_zip_is_malformed(tmp_path: Path) -> None:
    whole = office(tmp_path / "w", DOCX_TYPES, word__document_xml=b"<w/>").read_bytes()
    path = tmp_path / "cut"
    path.write_bytes(whole[: len(whole) // 2])
    assert rejected(path) == "malformed_file"


def test_oversized_content_types_part(tmp_path: Path) -> None:
    padded = DOCX_TYPES + " " * (2 * 1024 * 1024)
    assert rejected(office(tmp_path / "p", padded)) == "archive_limits_exceeded"
