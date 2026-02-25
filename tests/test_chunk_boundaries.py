"""TDD Gate — Phase 0 / Phase 2.

Chunk boundary invariants:
1. No chunk may cross a table boundary (tables are atomic).
2. Non-table chunks respect overlap = 100 tokens.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from chunking.splitter import SECChunker, count_tokens
from core.models import BlockType, FilingMetadata
from ingestion.sec_proxy_parser import SECProxyParser


class TestChunkBoundaries:
    def _get_chunks(self, proxy_html_path: Path, apple_meta: FilingMetadata):
        parser = SECProxyParser()
        blocks = parser.parse_with_metadata(proxy_html_path, apple_meta)
        chunker = SECChunker()
        return chunker.chunk_blocks(blocks)

    def test_table_chunks_are_atomic(self, proxy_html_path: Path, apple_meta: FilingMetadata) -> None:
        """Each table block must produce exactly one chunk."""
        parser = SECProxyParser()
        blocks = parser.parse_with_metadata(proxy_html_path, apple_meta)
        table_blocks = [b for b in blocks if b.type == BlockType.TABLE]
        chunker = SECChunker()
        chunks = chunker.chunk_blocks(blocks)
        table_chunks = [c for c in chunks if c.chunk_type == BlockType.TABLE]
        assert len(table_chunks) == len(table_blocks), (
            f"Expected {len(table_blocks)} table chunks, got {len(table_chunks)}"
        )

    def test_table_chunks_have_table_json(self, proxy_html_path: Path, apple_meta: FilingMetadata) -> None:
        chunks = self._get_chunks(proxy_html_path, apple_meta)
        table_chunks = [c for c in chunks if c.chunk_type == BlockType.TABLE]
        for tc in table_chunks:
            assert tc.table_json is not None, f"Chunk {tc.id} missing table_json"

    def test_chunk_indices_are_monotonic(self, proxy_html_path: Path, apple_meta: FilingMetadata) -> None:
        chunks = self._get_chunks(proxy_html_path, apple_meta)
        indices = [c.chunk_index for c in chunks]
        assert indices == list(range(len(chunks))), "Chunk indices are not monotonically increasing"
