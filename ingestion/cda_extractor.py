"""Extract full CD&A narrative text from parsed SEC proxy blocks."""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, TypeGuard

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
_FALLBACK_START_PATTERNS = [
    "executive compensation",
    "compensation",
]
_CDA_STORAGE_CHAR_CAP = 100_000
_MIN_CDA_STORAGE_CHAR_CAP = 50_000
_TERMINAL_CHARS = {".", "!", "?", '"', ")"}
_MIN_CDA_TOKEN_COUNT = 500


def _normalise(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def _is_text_block(block: BaseBlock) -> TypeGuard[ProseBlock | XBRLTaggedBlock]:
    return isinstance(block, (ProseBlock, XBRLTaggedBlock))


def _block_page_start(block: BaseBlock) -> int | None:
    if block.toc_page_range is None:
        return None
    return block.toc_page_range[0]


def _block_page_end(block: BaseBlock) -> int | None:
    if block.toc_page_range is None:
        return None
    return block.toc_page_range[1]


def _is_after_toc_end(block: BaseBlock, toc_end_page: int) -> bool:
    block_page_start = _block_page_start(block)
    return block_page_start is not None and block_page_start > toc_end_page


def _extract_primary_cda_parts(blocks: list[BaseBlock]) -> list[str]:
    in_cda = False
    cda_parts: list[str] = []
    toc_end_page: int | None = None

    for block in blocks:
        if isinstance(block, HeadingBlock):
            heading = _normalise(block.text)
            if any(pattern in heading for pattern in _CDA_START_PATTERNS):
                in_cda = True
                heading_page_end = _block_page_end(block)
                if heading_page_end is not None:
                    toc_end_page = heading_page_end
                continue
            if in_cda and any(pattern in heading for pattern in _CDA_END_PATTERNS):
                heading_page_start = _block_page_start(block)
                if heading_page_start is not None:
                    end_before_heading = max(0, heading_page_start - 1)
                    toc_end_page = (
                        end_before_heading
                        if toc_end_page is None
                        else min(toc_end_page, end_before_heading)
                    )
                break

        if in_cda and _is_text_block(block):
            if toc_end_page is not None and _is_after_toc_end(block, toc_end_page):
                break
            text = block.text.strip()
            if text:
                cda_parts.append(text)
                if toc_end_page is None:
                    block_page_end = _block_page_end(block)
                    if block_page_end is not None:
                        toc_end_page = block_page_end

    return cda_parts


def _extract_fallback_cda_parts(blocks: list[BaseBlock]) -> list[str]:
    """Fallback when heading-based CD&A boundaries are missing in the parsed HTML."""
    in_comp_section = False
    cda_parts: list[str] = []
    toc_end_page: int | None = None

    for block in blocks:
        if isinstance(block, HeadingBlock):
            heading = _normalise(block.text)
            if any(pattern in heading for pattern in _CDA_END_PATTERNS) and cda_parts:
                heading_page_start = _block_page_start(block)
                if heading_page_start is not None and toc_end_page is None:
                    toc_end_page = max(0, heading_page_start - 1)
                break
            if any(pattern in heading for pattern in _FALLBACK_START_PATTERNS):
                in_comp_section = True
                if toc_end_page is None:
                    heading_page_end = _block_page_end(block)
                    if heading_page_end is not None:
                        toc_end_page = heading_page_end
                continue

        if not _is_text_block(block):
            continue

        text = block.text.strip()
        if not text:
            continue

        normalized_text = _normalise(text)
        if any(pattern in normalized_text for pattern in _CDA_START_PATTERNS):
            in_comp_section = True
            if toc_end_page is None:
                text_page_end = _block_page_end(block)
                if text_page_end is not None:
                    toc_end_page = text_page_end

        if in_comp_section:
            if toc_end_page is not None and _is_after_toc_end(block, toc_end_page):
                break
            cda_parts.append(text)
            if toc_end_page is None:
                text_page_end = _block_page_end(block)
                if text_page_end is not None:
                    toc_end_page = text_page_end

    return cda_parts


def _truncate_to_sentence_boundary(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text

    candidate = text[:max_chars].rstrip()
    sentence_boundary = max(candidate.rfind("."), candidate.rfind("!"), candidate.rfind("?"))
    if sentence_boundary >= 0:
        sentence_end = sentence_boundary + 1
        while sentence_end < len(candidate) and candidate[sentence_end] in {'"', ")", "]"}:
            sentence_end += 1
        return candidate[:sentence_end].rstrip()

    word_boundary = candidate.rfind(" ")
    if word_boundary > 0:
        candidate = candidate[:word_boundary].rstrip()
    return f"{candidate}."


def _collect_all_text(blocks: list[BaseBlock]) -> str:
    parts: list[str] = []
    for block in blocks:
        if not _is_text_block(block):
            continue
        text = block.text.strip()
        if text:
            parts.append(text)
    return "\n\n".join(parts).strip()


def _expand_short_cda_text(blocks: list[BaseBlock], current_text: str) -> str:
    if len(current_text.split()) > _MIN_CDA_TOKEN_COUNT:
        return current_text

    compensation_parts: list[str] = []
    seen_compensation = False
    for block in blocks:
        if not _is_text_block(block):
            continue
        text = block.text.strip()
        if not text:
            continue
        normalized = _normalise(text)
        if "compensation" in normalized:
            seen_compensation = True
        if seen_compensation:
            compensation_parts.append(text)

    expanded = "\n\n".join(compensation_parts).strip()
    if len(expanded.split()) > len(current_text.split()):
        current_text = expanded

    if len(current_text.split()) > _MIN_CDA_TOKEN_COUNT:
        return current_text

    all_text = _collect_all_text(blocks)
    if len(all_text.split()) > len(current_text.split()):
        return all_text
    return current_text


def _ensure_sentence_end(text: str) -> str:
    normalized = text.rstrip()
    if not normalized:
        return ""
    if normalized[-1] in _TERMINAL_CHARS:
        return normalized

    sentence_boundary = max(normalized.rfind("."), normalized.rfind("!"), normalized.rfind("?"))
    if sentence_boundary >= 0:
        sentence_end = sentence_boundary + 1
        while sentence_end < len(normalized) and normalized[sentence_end] in {'"', ")", "]"}:
            sentence_end += 1
        return normalized[:sentence_end].rstrip()

    word_boundary = normalized.rfind(" ")
    if word_boundary > 0:
        normalized = normalized[:word_boundary].rstrip()
    return f"{normalized}."


def extract_cda(
    blocks: list[BaseBlock],
    meta: Mapping[str, Any],
) -> dict[str, Any]:
    """Return one row containing full CD&A prose and simple feature flags."""
    primary_parts = _extract_primary_cda_parts(blocks)
    cda_parts = primary_parts if primary_parts else _extract_fallback_cda_parts(blocks)

    # Capture the complete extracted block first, then enforce any storage cap.
    raw_full_text = "\n\n".join(cda_parts).strip()
    raw_full_text = _expand_short_cda_text(blocks, raw_full_text)
    storage_cap = max(_CDA_STORAGE_CHAR_CAP, _MIN_CDA_STORAGE_CHAR_CAP)
    capped_text = _truncate_to_sentence_boundary(raw_full_text, storage_cap)
    full_text = _ensure_sentence_end(capped_text)
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
