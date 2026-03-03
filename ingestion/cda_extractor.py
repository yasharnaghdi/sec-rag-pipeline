"""Extract full CD&A narrative text from parsed SEC proxy blocks."""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from ingestion.metadata_model import BaseBlock, HeadingBlock, ProseBlock, XBRLTaggedBlock

_CDA_START_PATTERNS = [
    "compensation discussion and analysis",
    "executive compensation",
]
_CDA_END_PATTERNS = [
    "summary compensation table",
    "report of the compensation committee",
    "compensation committee report",
]
_PFP_KEYWORDS = [
    "pay-for-performance",
    "pay for performance",
    "performance-based",
    "performance based",
    "pay and performance",
    "alignment of pay",
]


def _normalise(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def extract_cda(
    blocks: list[BaseBlock],
    meta: Mapping[str, Any],
) -> dict[str, Any]:
    """Return one row containing full CD&A prose and simple feature flags."""
    in_cda = False
    cda_parts: list[str] = []

    for block in blocks:
        if isinstance(block, HeadingBlock):
            heading = _normalise(block.text)
            if any(pattern in heading for pattern in _CDA_START_PATTERNS):
                in_cda = True
                continue
            if in_cda and any(pattern in heading for pattern in _CDA_END_PATTERNS):
                break

        if in_cda and isinstance(block, (ProseBlock, XBRLTaggedBlock)):
            if block.text.strip():
                cda_parts.append(block.text.strip())

    full_text = "\n\n".join(cda_parts)
    token_count = len(full_text.split())
    normalized_full_text = full_text.lower()
    pfp_flag = any(keyword in normalized_full_text for keyword in _PFP_KEYWORDS)

    return {
        **dict(meta),
        "cda_full_text": full_text,
        "cda_token_count": token_count,
        "pay_for_performance_flag": pfp_flag,
        "cda_section_found": bool(cda_parts),
    }
