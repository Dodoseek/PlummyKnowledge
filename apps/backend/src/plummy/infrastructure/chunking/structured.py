"""Structure-aware chunking (ARCHITECTURE.md §8.4).

- A chunk never crosses a section (heading path) or a sheet: its citation stays exact.
- A heading starts a chunk and stays with the text below it.
- Blocks are packed up to ``max_tokens``; spreadsheet and CSV rows are packed together with their row range.
- A block longer than ``max_tokens`` is split at paragraph, line, sentence or word boundaries, with
  ``overlap_tokens`` of the previous piece repeated at the start of the next one.

Pure and deterministic: no clock, randomness or dictionary-order dependence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Final

from plummy.application.ingestion.chunking import (
    MAX_CHUNKS_PER_VERSION,
    ChunkDraft,
    ChunkingError,
    ChunkingSettings,
    estimate_tokens,
)
from plummy.application.ingestion.parsing import BlockKind, DocumentBlock, NormalizedDocument

NAME: Final = "structured/1"
SEPARATOR: Final = "\n\n"
ROW_SEPARATOR: Final = "\n"
# Split points for a long block, coarse to fine; the last resort is a hard cut.
BOUNDARIES: Final = (
    re.compile(r"\n\s*\n"),
    re.compile(r"\n"),
    re.compile(r"(?<=[.!?…])\s+"),
    re.compile(r"\s+"),
)


@dataclass
class _Open:
    """The chunk being filled."""

    section_path: tuple[str, ...]
    sheet: str | None
    parts: list[str] = field(default_factory=list)
    tokens: int = 0
    pages: list[int] = field(default_factory=list)
    rows: list[int] = field(default_factory=list)
    rows_only: bool = True


class StructuredChunker:
    @property
    def name(self) -> str:
        return NAME

    def chunk(self, document: NormalizedDocument, settings: ChunkingSettings) -> list[ChunkDraft]:
        drafts: list[ChunkDraft] = []
        current: _Open | None = None

        def close() -> None:
            nonlocal current
            if current is not None and current.parts:
                separator = ROW_SEPARATOR if current.rows_only else SEPARATOR
                content = separator.join(current.parts)
                if len(drafts) >= MAX_CHUNKS_PER_VERSION:
                    raise ChunkingError("chunk_limit_exceeded", "The document produces too many chunks.")
                drafts.append(
                    ChunkDraft(
                        index=len(drafts),
                        content=content,
                        token_count=estimate_tokens(content),
                        page_start=min(current.pages) if current.pages else None,
                        page_end=max(current.pages) if current.pages else None,
                        section_path=current.section_path,
                        sheet_name=current.sheet,
                        row_start=min(current.rows) if current.rows else None,
                        row_end=max(current.rows) if current.rows else None,
                    )
                )
            current = None

        for block in document.blocks:
            text = block.text.strip()
            if not text:
                continue
            context = (block.section_path, block.sheet)
            if block.kind is BlockKind.HEADING or (
                current and (current.section_path, current.sheet) != context
            ):
                close()
            for piece in _pieces(text, settings):
                tokens = estimate_tokens(piece)
                if current is not None and current.tokens + tokens > settings.max_tokens:
                    close()
                if current is None:
                    current = _Open(section_path=block.section_path, sheet=block.sheet)
                current.parts.append(piece)
                current.tokens += tokens + 1
                current.rows_only = current.rows_only and block.kind is BlockKind.ROW
                _add_position(current, block)
        close()
        return drafts


def _add_position(chunk: _Open, block: DocumentBlock) -> None:
    if block.page is not None:
        chunk.pages.append(block.page)
    if block.row is not None:
        chunk.rows.append(block.row)


def _pieces(text: str, settings: ChunkingSettings) -> list[str]:
    """``text`` itself if it fits, otherwise pieces of at most ``max_tokens`` with overlap."""
    if estimate_tokens(text) <= settings.max_tokens:
        return [text]
    pieces: list[str] = []
    budget = settings.max_tokens - settings.overlap_tokens - 1  # the joining space
    for unit in _units(text, budget):
        if pieces and settings.overlap_tokens:
            unit = _tail(pieces[-1], settings.overlap_tokens) + " " + unit
        pieces.append(unit.strip())
    return pieces


def _units(text: str, budget: int, level: int = 0) -> list[str]:
    """Greedy packing of the parts between boundaries into units of at most ``budget`` tokens."""
    if estimate_tokens(text) <= budget:
        return [text]
    if level >= len(BOUNDARIES):
        return _hard_cut(text, budget)
    units: list[str] = []
    buffer = ""
    for part in BOUNDARIES[level].split(text):
        part = part.strip()
        if not part:
            continue
        if estimate_tokens(part) > budget:
            if buffer:
                units.append(buffer)
                buffer = ""
            units.extend(_units(part, budget, level + 1))
            continue
        candidate = f"{buffer} {part}" if buffer else part
        if estimate_tokens(candidate) <= budget:
            buffer = candidate
        else:
            units.append(buffer)
            buffer = part
    if buffer:
        units.append(buffer)
    return units


def _hard_cut(text: str, budget: int) -> list[str]:
    """A word longer than the budget (base64, a URL): cut by bytes at character boundaries."""
    units, buffer = [], ""
    for character in text:
        if estimate_tokens(buffer + character) > budget:
            units.append(buffer)
            buffer = ""
        buffer += character
    if buffer:
        units.append(buffer)
    return units


def _tail(text: str, tokens: int) -> str:
    """The last words of ``text`` within ``tokens``."""
    words = text.split()
    tail: list[str] = []
    for word in reversed(words):
        if estimate_tokens(" ".join([word, *tail])) > tokens:
            break
        tail.insert(0, word)
    return " ".join(tail)
