"""SEC-aware chunker for typed ``SECBlock`` inputs.

Rules:
- Tables are atomic and emitted as exactly one chunk.
- Non-table text respects configured chunk size and overlap.
- Chunk indices are document-scoped and monotonically increasing.
- Token counting stays deterministic and offline-safe.
"""
from __future__ import annotations

from typing import Any

import tiktoken
from langchain_text_splitters import RecursiveCharacterTextSplitter

from core.config import get_settings
from core.models import BlockType, Chunk, SECBlock

_ENCODING: Any | None = None
_ENCODING_INITIALIZED = False


def _get_encoding() -> Any | None:
    """Return ``cl100k_base`` when available, otherwise fall back offline."""
    global _ENCODING
    global _ENCODING_INITIALIZED

    if _ENCODING_INITIALIZED:
        return _ENCODING

    try:
        _ENCODING = tiktoken.get_encoding("cl100k_base")
    except Exception:
        _ENCODING = None
    _ENCODING_INITIALIZED = True
    return _ENCODING


def count_tokens(text: str) -> int:
    """Count tokens deterministically without requiring network access."""
    if not text:
        return 0

    encoding = _get_encoding()
    if encoding is None:
        return len(text.split())
    return len(encoding.encode(text))


class SECChunker:
    """Splits SECBlocks into Chunks suitable for embedding."""

    def __init__(self) -> None:
        s = get_settings()
        self._splitter = RecursiveCharacterTextSplitter(
            separators=["\n\n", "\n", ". ", "? ", "! ", "; ", ", ", " ", ""],
            chunk_size=s.chunk_size_tokens,
            chunk_overlap=s.chunk_overlap_tokens,
            length_function=count_tokens,
        )
        self._chunk_size = s.chunk_size_tokens

    def chunk_blocks(self, blocks: list[SECBlock]) -> list[Chunk]:
        """Chunk SEC blocks while preserving table boundaries."""
        chunks: list[Chunk] = []
        index = 0

        for block in blocks:
            if block.type == BlockType.TABLE:
                table_text = block.linearized_text or block.text
                # Tables are atomic — never split
                chunks.append(
                    Chunk(
                        source_block_id=block.id,
                        section_id=None,
                        text=table_text,
                        chunk_type=BlockType.TABLE,
                        token_count=count_tokens(table_text),
                        chunk_index=index,
                        table_json=block.rows,
                        linearized_text=block.linearized_text,
                    )
                )
                index += 1
                continue

            block_text = block.text.strip()
            if not block_text:
                continue

            splits = self._splitter.split_text(block_text)
            for split_text in splits:
                cleaned_split = split_text.strip()
                if not cleaned_split:
                    continue
                chunks.append(
                    Chunk(
                        source_block_id=block.id,
                        section_id=None,
                        text=cleaned_split,
                        chunk_type=block.type,
                        token_count=count_tokens(cleaned_split),
                        chunk_index=index,
                    )
                )
                index += 1

        return chunks
