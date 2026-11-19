"""``ChunkingStrategy`` port (ARCHITECTURE.md §8.4): stage 8 of the pipeline (ADR-008).

Chunks keep their position (pages, section path, sheet, rows) for citations. Chunking is deterministic:
the same ``NormalizedDocument`` and settings give the same ``chunk_index``, text and ``content_hash``, so a
retried job writes the same rows (unique ``(document_version_id, chunk_set, chunk_index)``).
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Protocol

from plummy.application.ingestion.parsing import NormalizedDocument

MAX_CHUNKS_PER_VERSION: Final = 50_000


class ChunkingError(Exception):
    """Permanent: the document cannot be chunked within the limits."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def estimate_tokens(text: str) -> int:
    """Approximate token count without a model tokenizer: about 4 UTF-8 bytes per token.

    BPE tokenizers give ~4 characters per token for English and ~2 for Cyrillic (2 bytes per letter), so the
    byte count tracks both. Used for chunk sizes and the context budget, never for billing.
    """
    return math.ceil(len(text.encode("utf-8")) / 4)


@dataclass(frozen=True, slots=True)
class ChunkingSettings:
    """[Proposed] defaults (DATABASE_SCHEMA.md: ``knowledge_bases.chunking_settings``)."""

    max_tokens: int = 512
    overlap_tokens: int = 64  # only between pieces of one block that was too long

    def __post_init__(self) -> None:
        if not 64 <= self.max_tokens <= 8192:
            raise ValueError("max_tokens must be 64-8192")
        if not 0 <= self.overlap_tokens <= self.max_tokens // 2:
            raise ValueError("overlap_tokens must be 0 to max_tokens/2")

    @classmethod
    def from_mapping(cls, data: Mapping[str, object]) -> ChunkingSettings:
        """Missing keys take the defaults; unknown keys are ignored (settings stored by a newer version)."""
        defaults = cls()
        values: dict[str, int] = {}
        for name in ("max_tokens", "overlap_tokens"):
            value = data.get(name, getattr(defaults, name))
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"{name} must be an integer")
            values[name] = value
        return cls(**values)

    def limited_to(self, max_input_tokens: int | None) -> ChunkingSettings:
        """A chunk must fit the embedding model input."""
        if max_input_tokens is None or max_input_tokens >= self.max_tokens:
            return self
        size = max(64, max_input_tokens)
        return ChunkingSettings(max_tokens=size, overlap_tokens=min(self.overlap_tokens, size // 2))

    def to_json(self) -> dict[str, int]:
        return {"max_tokens": self.max_tokens, "overlap_tokens": self.overlap_tokens}


@dataclass(frozen=True, slots=True)
class ChunkDraft:
    index: int
    content: str
    token_count: int
    page_start: int | None = None
    page_end: int | None = None
    section_path: tuple[str, ...] = ()
    sheet_name: str | None = None
    row_start: int | None = None
    row_end: int | None = None

    @property
    def content_hash(self) -> bytes:
        return hashlib.sha256(self.content.encode("utf-8")).digest()


class ChunkingStrategy(Protocol):
    @property
    def name(self) -> str: ...

    def chunk(self, document: NormalizedDocument, settings: ChunkingSettings) -> list[ChunkDraft]:
        """Raises :class:`ChunkingError`."""
        ...
