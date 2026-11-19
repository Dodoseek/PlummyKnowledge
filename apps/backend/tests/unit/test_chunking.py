"""StructuredChunker and ChunkingSettings: structure, positions, sizes, overlap, determinism, limits."""

from __future__ import annotations

import itertools

import pytest

from plummy.application.ingestion import chunking
from plummy.application.ingestion.chunking import ChunkingError, ChunkingSettings, estimate_tokens
from plummy.application.ingestion.parsing import BlockKind, DocumentBlock, NormalizedDocument
from plummy.infrastructure.chunking.structured import StructuredChunker

SMALL = ChunkingSettings(max_tokens=64, overlap_tokens=8)


def doc(*blocks: DocumentBlock) -> NormalizedDocument:
    return NormalizedDocument(blocks=blocks)


def chunk(
    document: NormalizedDocument, settings: ChunkingSettings | None = None
) -> list[chunking.ChunkDraft]:
    return StructuredChunker().chunk(document, settings or ChunkingSettings())


def test_token_estimate_counts_bytes() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2
    assert estimate_tokens("жж") == 1  # 4 bytes


def test_sections_start_new_chunks_and_keep_pages() -> None:
    policy, leave = ("Policy",), ("Policy", "Leave")
    drafts = chunk(
        doc(
            DocumentBlock(BlockKind.HEADING, "Policy", page=1, section_path=policy, level=1),
            DocumentBlock(BlockKind.PARAGRAPH, "Intro.", page=1, section_path=policy),
            DocumentBlock(BlockKind.PARAGRAPH, "More intro.", page=2, section_path=policy),
            DocumentBlock(BlockKind.HEADING, "Leave", page=2, section_path=leave, level=2),
            DocumentBlock(BlockKind.PARAGRAPH, "28 days.", page=3, section_path=leave),
            DocumentBlock(BlockKind.PARAGRAPH, "   ", page=3, section_path=leave),
        )
    )
    assert [(d.index, d.content, d.section_path, d.page_start, d.page_end) for d in drafts] == [
        (0, "Policy\n\nIntro.\n\nMore intro.", policy, 1, 2),
        (1, "Leave\n\n28 days.", leave, 2, 3),
    ]
    assert drafts[0].token_count == estimate_tokens(drafts[0].content)
    assert len(drafts[0].content_hash) == 32


def test_rows_are_packed_per_sheet_with_row_ranges() -> None:
    rows = [
        DocumentBlock(BlockKind.ROW, f"Item: {n}; Cost: {n * 10}", sheet="A", row=n) for n in range(2, 40)
    ]
    other = DocumentBlock(BlockKind.ROW, "Item: x", sheet="B", row=2)
    drafts = chunk(doc(*rows, other), SMALL)
    assert all(d.token_count <= SMALL.max_tokens for d in drafts)
    a_chunks = [d for d in drafts if d.sheet_name == "A"]
    assert a_chunks[0].row_start == 2
    assert a_chunks[-1].row_end == 39
    assert [d.row_start for d in a_chunks[1:]] == [d.row_end + 1 for d in a_chunks[:-1] if d.row_end]
    assert "\n" in a_chunks[0].content
    assert "\n\n" not in a_chunks[0].content
    assert (drafts[-1].sheet_name, drafts[-1].row_start, drafts[-1].row_end) == ("B", 2, 2)


def test_long_block_is_split_with_overlap_and_within_the_limit() -> None:
    sentences = " ".join(f"Sentence number {n} talks about leave." for n in range(60))
    drafts = chunk(doc(DocumentBlock(BlockKind.PARAGRAPH, sentences, page=4)), SMALL)
    assert len(drafts) > 3
    assert all(d.token_count <= SMALL.max_tokens for d in drafts)
    assert all(d.page_start == d.page_end == 4 for d in drafts)
    for previous, current in itertools.pairwise(drafts):
        last_word = previous.content.split()[-1]
        assert last_word in current.content.split()[:6]  # overlap carries the end of the previous piece
    covered = " ".join(d.content for d in drafts)
    assert all(f"number {n} " in covered for n in range(60))


def test_word_longer_than_a_chunk_is_cut() -> None:
    blob = "x" * 2000
    drafts = chunk(doc(DocumentBlock(BlockKind.PARAGRAPH, blob)), SMALL)
    assert all(d.token_count <= SMALL.max_tokens for d in drafts)
    assert "".join(d.content.replace(" ", "") for d in drafts).count("x") >= 2000


def test_chunking_is_deterministic() -> None:
    blocks = tuple(
        DocumentBlock(
            BlockKind.PARAGRAPH, f"Paragraph {n} " * 20, page=n // 3 + 1, section_path=(f"S{n // 5}",)
        )
        for n in range(30)
    )
    first, second = chunk(doc(*blocks), SMALL), chunk(doc(*blocks), SMALL)
    assert first == second
    assert [d.content_hash for d in first] == [d.content_hash for d in second]
    assert [d.index for d in first] == list(range(len(first)))


def test_empty_document_gives_no_chunks() -> None:
    assert chunk(doc(DocumentBlock(BlockKind.PARAGRAPH, " \n "))) == []


def test_chunk_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("plummy.infrastructure.chunking.structured.MAX_CHUNKS_PER_VERSION", 3)
    headings = [DocumentBlock(BlockKind.HEADING, f"H{n}", section_path=(f"H{n}",)) for n in range(4)]
    with pytest.raises(ChunkingError) as error:
        chunk(doc(*headings))
    assert error.value.code == "chunk_limit_exceeded"
    assert len(chunk(doc(*headings[:3]))) == 3


def test_settings_defaults_validation_and_model_limit() -> None:
    assert ChunkingSettings.from_mapping({}) == ChunkingSettings(max_tokens=512, overlap_tokens=64)
    assert ChunkingSettings.from_mapping({"max_tokens": 256, "unknown": 1}).max_tokens == 256
    for bad in ({"max_tokens": 10}, {"max_tokens": True}, {"overlap_tokens": 300}, {"max_tokens": "512"}):
        with pytest.raises(ValueError, match="tokens"):
            ChunkingSettings.from_mapping(bad)
    limited = ChunkingSettings().limited_to(100)
    assert (limited.max_tokens, limited.overlap_tokens) == (100, 50)
    assert ChunkingSettings().limited_to(None) == ChunkingSettings()
    assert ChunkingSettings().limited_to(8000) == ChunkingSettings()
    assert ChunkingSettings().limited_to(10).max_tokens == 64
    assert ChunkingSettings().to_json() == {"max_tokens": 512, "overlap_tokens": 64}
