"""TDD Gate — Phase 0 / Phase 1.

Every table block must carry BOTH rows (JSON) AND linearized_text (for embedding).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.models import BlockType, FilingMetadata
from ingestion.sec_proxy_parser import SECProxyParser


class TestTableDualFormat:
    def test_table_blocks_have_rows(self, proxy_html_path: Path, apple_meta: FilingMetadata) -> None:
        parser = SECProxyParser()
        blocks = parser.parse_with_metadata(proxy_html_path, apple_meta)
        table_blocks = [b for b in blocks if b.type == BlockType.TABLE]
        assert len(table_blocks) >= 1, "No table blocks found in proxy HTML"
        for tb in table_blocks:
            assert tb.rows is not None, f"Table block {tb.id} missing rows"
            assert len(tb.rows) > 0

    def test_table_blocks_have_linearized_text(self, proxy_html_path: Path, apple_meta: FilingMetadata) -> None:
        parser = SECProxyParser()
        blocks = parser.parse_with_metadata(proxy_html_path, apple_meta)
        table_blocks = [b for b in blocks if b.type == BlockType.TABLE]
        for tb in table_blocks:
            assert tb.linearized_text is not None, f"Table block {tb.id} missing linearized_text"
            assert len(tb.linearized_text) > 0

    def test_table_rows_is_list_of_lists(self, proxy_html_path: Path, apple_meta: FilingMetadata) -> None:
        parser = SECProxyParser()
        blocks = parser.parse_with_metadata(proxy_html_path, apple_meta)
        table_blocks = [b for b in blocks if b.type == BlockType.TABLE]
        for tb in table_blocks:
            assert isinstance(tb.rows, list)
            for row in tb.rows:  # type: ignore[union-attr]
                assert isinstance(row, list)
                assert all(isinstance(cell, str) for cell in row)
