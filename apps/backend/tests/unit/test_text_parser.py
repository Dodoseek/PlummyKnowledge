"""PlainTextParser: TXT paragraphs, Markdown structure, CSV rows with their numbers, limits."""

from __future__ import annotations

from pathlib import Path

import pytest

from plummy.application.ingestion.parsing import BlockKind, NormalizedDocument, ParseError, ParseLimits
from plummy.domain.file_types import DocumentFormat
from plummy.infrastructure.parsing.text import PlainTextParser

LIMITS = ParseLimits()


async def parse(
    tmp_path: Path, text: str, file_format: DocumentFormat, limits: ParseLimits = LIMITS
) -> NormalizedDocument:
    path = tmp_path / "input"
    path.write_bytes(text.encode())
    return await PlainTextParser().parse(path, file_format, limits)


def shape(document: NormalizedDocument) -> list[tuple[str, str]]:
    return [(block.kind.value, block.text) for block in document.blocks]


async def test_txt_paragraphs(tmp_path: Path) -> None:
    text = "\ufeff" + "\r\n".join(["Первый абзац", "продолжение.", "", "", "  Второй.  ", ""])
    document = await parse(tmp_path, text, DocumentFormat.TXT)
    assert shape(document) == [
        ("paragraph", "\n".join(["Первый абзац", "продолжение."])),
        ("paragraph", "Второй."),
    ]
    assert document.page_count is None


async def test_markdown_headings_sections_lists_and_code(tmp_path: Path) -> None:
    text = (
        "Intro line\n\n# Policy #\n\nText under policy.\n\n## Leave\n\n- one\n- two\n\n"
        "```python\nx = 1\n\n# not a heading\n```\n\n# Other\n\n####### seven hashes is text\n"
    )
    document = await parse(tmp_path, text, DocumentFormat.MARKDOWN)
    assert [(b.kind, b.text, b.section_path, b.level) for b in document.blocks] == [
        (BlockKind.PARAGRAPH, "Intro line", (), None),
        (BlockKind.HEADING, "Policy", ("Policy",), 1),
        (BlockKind.PARAGRAPH, "Text under policy.", ("Policy",), None),
        (BlockKind.HEADING, "Leave", ("Policy", "Leave"), 2),
        (BlockKind.LIST_ITEM, "- one\n- two", ("Policy", "Leave"), None),
        (BlockKind.CODE, "```python\nx = 1\n\n# not a heading\n```", ("Policy", "Leave"), None),
        (BlockKind.HEADING, "Other", ("Other",), 1),
        (BlockKind.PARAGRAPH, "####### seven hashes is text", ("Other",), None),
    ]


async def test_markdown_unclosed_fence_and_long_heading(tmp_path: Path) -> None:
    document = await parse(
        tmp_path, "# " + "H" * 500 + "\n\n```\ncode without end\n", DocumentFormat.MARKDOWN
    )
    heading, code = document.blocks
    assert (heading.kind, len(heading.text)) == (BlockKind.HEADING, 200)
    assert (code.kind, code.text) == (BlockKind.CODE, "```\ncode without end")


async def test_csv_rows_keep_header_names_and_line_numbers(tmp_path: Path) -> None:
    text = 'Name;City;Note\nAnna;Paris;"multi\nline"\n\n;;\nBob;;extra;more\n'
    document = await parse(tmp_path, text, DocumentFormat.CSV)
    assert [(b.kind, b.text, b.row) for b in document.blocks] == [
        (BlockKind.ROW, "Name: Anna; City: Paris; Note: multi\nline", 3),
        (BlockKind.ROW, "Name: Bob; Note: extra; column 4: more", 6),
    ]


async def test_csv_header_only_is_still_text(tmp_path: Path) -> None:
    document = await parse(tmp_path, "a,b,c\n", DocumentFormat.CSV)
    assert shape(document) == [("row", "a; b; c")]


async def test_csv_row_limit(tmp_path: Path) -> None:
    text = "h\n" + "".join(f"{n}\n" for n in range(11))
    with pytest.raises(ParseError) as error:
        await parse(tmp_path, text, DocumentFormat.CSV, ParseLimits(max_rows=10))
    assert error.value.code == "row_limit_exceeded"
    assert len((await parse(tmp_path, text, DocumentFormat.CSV, ParseLimits(max_rows=11))).blocks) == 11


@pytest.mark.parametrize("file_format", [DocumentFormat.TXT, DocumentFormat.MARKDOWN, DocumentFormat.CSV])
async def test_blank_documents_have_no_text(tmp_path: Path, file_format: DocumentFormat) -> None:
    with pytest.raises(ParseError) as error:
        await parse(tmp_path, " \n\n\t\n", file_format)
    assert error.value.code == "no_text"


async def test_text_limit_by_characters_and_by_file_size(tmp_path: Path) -> None:
    with pytest.raises(ParseError) as error:
        await parse(tmp_path, "я" * 11, DocumentFormat.TXT, ParseLimits(max_text_chars=10))
    assert error.value.code == "text_limit_exceeded"
    # rejected by size before the file is read: more bytes than 4 per allowed character
    with pytest.raises(ParseError) as error:
        await parse(tmp_path, "a" * 41, DocumentFormat.TXT, ParseLimits(max_text_chars=10))
    assert error.value.code == "text_limit_exceeded"
    assert shape(await parse(tmp_path, "я" * 10, DocumentFormat.TXT, ParseLimits(max_text_chars=10)))


def test_formats_and_name() -> None:
    parser = PlainTextParser()
    assert parser.name == "plummy-text/1"
    assert parser.supported_formats() == {DocumentFormat.TXT, DocumentFormat.MARKDOWN, DocumentFormat.CSV}
