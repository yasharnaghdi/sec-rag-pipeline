"""SEC proxy filing parser — extends HTMLParser with SEC section detection and
Filing metadata injection.

TODO (Phase 1 implementation):
- Implement _detect_section_header() using regex pattern matching
- Implement _tag_footnotes() using <sup> and footnote section detection
- Wire edgartools FilingMetadata at download time
"""
from __future__ import annotations

import re
from pathlib import Path

from core.models import BlockType, ContentBlock, FilingMetadata, SECBlock
from ingestion.html_parser import HTMLParser

# DEF 14A standard section patterns (case-insensitive)
SEC_SECTION_PATTERNS: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE)
    for p in [
        r"executive compensation",
        r"board of directors",
        r"audit committee",
        r"related[\s\-]party transactions",
        r"security ownership",
        r"say[\s\-]on[\s\-]pay",
        r"corporate governance",
        r"director independence",
        r"compensation discussion",
        r"risk oversight",
    ]
]


class SECProxyParser(HTMLParser):
    """Enriches base HTML blocks with SEC filing metadata and section attribution."""

    def parse_with_metadata(  # type: ignore[override]
        self,
        file_path: Path,
        metadata: FilingMetadata,
    ) -> list[SECBlock]:
        blocks = super().parse(file_path)
        return self._enrich_blocks(blocks, metadata)

    def _enrich_blocks(
        self,
        blocks: list[ContentBlock],
        meta: FilingMetadata,
    ) -> list[SECBlock]:
        sec_blocks: list[SECBlock] = []
        current_section: str | None = None
        section_order = 0

        for block in blocks:
            # Update running section context from headings
            if block.type == BlockType.HEADING:
                detected = self._detect_section_header(block.text)
                if detected:
                    current_section = detected
                    section_order += 1

            rows: list[list[str]] | None = None
            linearized: str | None = None
            if block.type == BlockType.TABLE:
                rows = block.metadata.get("rows")
                linearized = block.text  # already pipe-delimited

            sec_blocks.append(
                SECBlock(
                    id=block.id,
                    type=block.type,
                    text=block.text,
                    metadata=block.metadata,
                    cik=meta.cik,
                    company_name=meta.company_name,
                    filing_date=meta.filing_date,
                    document_type=meta.document_type,
                    section_header=current_section,
                    section_order=section_order,
                    rows=rows,
                    linearized_text=linearized,
                )
            )

        return sec_blocks

    @staticmethod
    def _detect_section_header(text: str) -> str | None:
        """Return normalised section label if text matches a known SEC section pattern."""
        for pattern in SEC_SECTION_PATTERNS:
            if pattern.search(text):
                # Normalise: title-case the matched text
                return text.strip().title()[:120]
        return None
