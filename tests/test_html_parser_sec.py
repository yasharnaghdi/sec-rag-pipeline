"""TDD Gate — Phase 0.

These tests MUST FAIL until Phase 1 (SECProxyParser) is implemented.
Do not implement — they are the CI contract.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from core.models import BlockType, FilingMetadata, SECBlock
from ingestion.sec_proxy_parser import SECProxyParser


class TestSECProxyParser:
    def test_blocks_have_cik(self, proxy_html_path: Path, apple_meta: FilingMetadata) -> None:
        parser = SECProxyParser()
        blocks = parser.parse_with_metadata(proxy_html_path, apple_meta)
        assert len(blocks) > 0
        assert all(b.cik == "0000320193" for b in blocks)

    def test_blocks_have_filing_date(self, proxy_html_path: Path, apple_meta: FilingMetadata) -> None:
        parser = SECProxyParser()
        blocks = parser.parse_with_metadata(proxy_html_path, apple_meta)
        assert all(b.filing_date is not None for b in blocks)

    def test_section_header_detected(self, proxy_html_path: Path, apple_meta: FilingMetadata) -> None:
        """Executive Compensation heading must trigger section detection."""
        parser = SECProxyParser()
        blocks = parser.parse_with_metadata(proxy_html_path, apple_meta)
        section_headers = {b.section_header for b in blocks if b.section_header}
        assert any("Executive Compensation" in h for h in section_headers), (
            f"Expected 'Executive Compensation' in section headers, got: {section_headers}"
        )

    def test_blocks_carry_company_name(self, proxy_html_path: Path, apple_meta: FilingMetadata) -> None:
        parser = SECProxyParser()
        blocks = parser.parse_with_metadata(proxy_html_path, apple_meta)
        assert all(b.company_name == "Apple Inc." for b in blocks)

    def test_returns_sec_block_instances(self, proxy_html_path: Path, apple_meta: FilingMetadata) -> None:
        parser = SECProxyParser()
        blocks = parser.parse_with_metadata(proxy_html_path, apple_meta)
        assert all(isinstance(b, SECBlock) for b in blocks)
