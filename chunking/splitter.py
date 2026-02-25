"""SEC-aware chunker.

Rules:
- Tables are NEVER split — emitted as atomic chunks regardless of token count.
- Paragraphs exceeding chunk_size_tokens use RecursiveCharacterTextSplitter
  with chunk_overlap_tokens overlap.
- Chunk index is document-scoped and monotonically increasing.

TODO (Phase 2 implementation):
- Integrate tiktoken token counting
- Wire LangChain RecursiveCharacterTextSplitter
- Add table boundary assertion (tested in test_chunk_boundaries.py)
"""
from __future__ import annotations

from uuid import uuid4

import tiktoken
from langchain_text_splitters import RecursiveCharacterTextSplitter

from core.config import get_settings
from core.models import BlockType, Chunk, SECBlock

_ENCODING = tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    return len(_ENCODING.encode(text))


class SECChunker:
    """Splits SECBlocks into Chunks suitable for embedding."""

    def __init__(self) -> None:
        s = get_settings()
        self._splitter = RecursiveCharacterTextSplitter.from_tiktoken_encoder(
            encoding_name="cl100k_base",
            chunk_size=s.chunk_size_tokens,
            chunk_overlap=s.chunk_overlap_tokens,
        )
        self._chunk_size = s.chunk_size_tokens

    def chunk_blocks(self, blocks: list[SECBlock]) -> list[Chunk]:
        chunks: list[Chunk] = []
        index = 0

        for block in blocks:
            if block.type == BlockType.TABLE:
                # Tables are atomic — never split
                chunks.append(
                    Chunk(
                        source_block_id=block.id,
                        text=block.linearized_text or block.text,
                        chunk_type=BlockType.TABLE,
                        token_count=count_tokens(block.linearized_text or block.text),
                        chunk_index=index,
                        table_json=block.rows,
                        linearized_text=block.linearized_text,
                    )
                )
                index += 1
            else:
                splits = self._splitter.split_text(block.text)
                for split_text in splits:
                    chunks.append(
                        Chunk(
                            source_block_id=block.id,
                            text=split_text,
                            chunk_type=block.type,
                            token_count=count_tokens(split_text),
                            chunk_index=index,
                        )
                    )
                    index += 1

        return chunks
