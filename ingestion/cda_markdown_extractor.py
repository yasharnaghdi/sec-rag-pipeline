"""Deterministic SEC section markdown extraction with ToC and heading fallbacks.

This module is intentionally independent from ``ingestion.cda_extractor``.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
import warnings
from dataclasses import dataclass
from datetime import date
from difflib import SequenceMatcher
from typing import Any, Literal

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from bs4.element import Tag
from pydantic import BaseModel, Field

from ingestion.metadata_model import DocumentMetadata

log = logging.getLogger(__name__)

_PAGE_TAIL_PATTERN = re.compile(r"\b(\d{1,3})\s*$")
_ITEM_PATTERN = re.compile(r"^ITEM\s+(\d+)\b", re.IGNORECASE)
_SEARCH_TAGS: tuple[str, ...] = ("h1", "h2", "h3", "h4", "h5", "h6", "p", "table", "ul", "ol", "div")
_CONTENT_BLOCK_TAGS: tuple[str, ...] = ("h1", "h2", "h3", "h4", "h5", "h6", "p", "table", "ul", "ol")
_LIST_TAGS: tuple[str, ...] = ("ul", "ol")
_HEADING_TAGS: tuple[str, ...] = ("h1", "h2", "h3", "h4", "h5", "h6")
_MIN_WORDS_GUARD = 120
_FRONT_MATTER_TABLE_LIMIT = 80

_EXEC_COMP_TERMS = (
    "EXECUTIVE COMPENSATION",
    "COMPENSATION DISCUSSION",
    "SECTION 1",
    "SECTION 2",
    "SECTION 3",
    "SECTION 4",
    "SECTION 5",
    "SECTION 6",
    "SECTION 7",
    "COMPENSATION TABLES",
    "EQUITY COMPENSATION PLANS",
    "PAY RATIO",
    "PAY VERSUS PERFORMANCE",
    "SAY-ON-PAY",
    "COMPENSATION AND BENEFITS COMMITTEE",
)

_NON_EXEC_TERMS = (
    "SHAREHOLDER PROPOSAL",
    "SHAREHOLDER PROPOSALS",
    "RATIFICATION",
    "AUDITOR",
    "AUDIT",
    "SAY-ON-FREQUENCY",
    "FREQUENCY",
    "OTHER BUSINESS",
)


@dataclass(frozen=True)
class _TocEntry:
    label: str
    normalized_label: str
    page: int
    href: str | None
    table_order: int
    row_order: int


@dataclass(frozen=True)
class SectionSpec:
    section_name: str
    aliases: tuple[str, ...]
    end_mode: Literal["major_non_exec", "next_boundary"]
    min_start_score: float = 0.62
    min_heading_score: float = 0.60
    min_keyword_score: float = 0.70
    required_tokens: tuple[str, ...] = ()
    min_required_token_hits: int = 0
    short_word_guard: int = _MIN_WORDS_GUARD


_SECTION_SPECS: tuple[SectionSpec, ...] = (
    SectionSpec(
        section_name="compensation_discussion_and_analysis",
        aliases=(
            "COMPENSATION DISCUSSION AND ANALYSIS",
            "COMPENSATION DISCUSSION",
            "CD&A",
            "CDA",
        ),
        end_mode="major_non_exec",
        min_start_score=0.62,
        min_heading_score=0.60,
        min_keyword_score=0.70,
        required_tokens=("COMPENSATION", "DISCUSSION", "ANALYSIS", "CD&A", "CDA"),
        min_required_token_hits=2,
    ),
    SectionSpec(
        section_name="executive_compensation",
        aliases=(
            "EXECUTIVE COMPENSATION",
            "EXECUTIVE COMPENSATION DISCUSSION",
        ),
        end_mode="major_non_exec",
        min_start_score=0.64,
        min_heading_score=0.62,
        min_keyword_score=0.74,
        required_tokens=("EXECUTIVE", "COMPENSATION"),
        min_required_token_hits=2,
    ),
    SectionSpec(
        section_name="director_compensation",
        aliases=(
            "DIRECTOR COMPENSATION",
            "COMPENSATION OF DIRECTORS",
        ),
        end_mode="next_boundary",
        min_start_score=0.64,
        min_heading_score=0.62,
        min_keyword_score=0.74,
        required_tokens=("DIRECTOR", "COMPENSATION"),
        min_required_token_hits=2,
    ),
    SectionSpec(
        section_name="pay_vs_performance",
        aliases=(
            "PAY VERSUS PERFORMANCE",
            "PAY VS PERFORMANCE",
        ),
        end_mode="next_boundary",
        min_start_score=0.62,
        min_heading_score=0.60,
        min_keyword_score=0.72,
        required_tokens=("PAY", "PERFORMANCE"),
        min_required_token_hits=2,
    ),
    SectionSpec(
        section_name="equity_compensation_plans",
        aliases=(
            "EQUITY COMPENSATION PLANS",
            "EQUITY COMPENSATION PLAN",
        ),
        end_mode="next_boundary",
        min_start_score=0.62,
        min_heading_score=0.60,
        min_keyword_score=0.72,
        required_tokens=("EQUITY", "COMPENSATION", "PLAN", "PLANS"),
        min_required_token_hits=2,
    ),
)

SECTION_SPECS: dict[str, SectionSpec] = {spec.section_name: spec for spec in _SECTION_SPECS}
SECTION_NAMES: tuple[str, ...] = tuple(spec.section_name for spec in _SECTION_SPECS)


class CDAExtractionResult(BaseModel):
    """Structured section extraction output."""

    section_name: str
    section_key: str | None = None
    section_found: bool = False
    markdown: str
    start_anchor: str | None = None
    end_anchor: str | None = None
    start_page: int | None = None
    end_page: int | None = None
    strategy: str
    warnings: list[str] = Field(default_factory=list)
    confidence: float


def extract_section_markdown(
    raw_html: str,
    metadata: DocumentMetadata | None = None,
    section_name: str = "compensation_discussion_and_analysis",
) -> CDAExtractionResult:
    """Extract a target SEC section as markdown with deterministic fallbacks."""
    spec = _get_section_spec(section_name)
    section_key = _build_section_key(metadata, section_name)

    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
    soup = BeautifulSoup(raw_html, "lxml")

    strategy_steps: list[str] = []
    extraction_warnings: list[str] = []
    confidence = 0.4

    toc_entries, toc_table_ids = _build_front_matter_toc(soup)
    start_entry = _resolve_section_toc_entry(toc_entries, spec)
    start_node: Tag | None = None
    start_anchor: str | None = None
    start_page: int | None = None

    if start_entry is not None:
        start_page = start_entry.page
        start_anchor = start_entry.href.lstrip("#") if start_entry.href else None
        start_node = _resolve_anchor_node(soup, start_entry.href)
        if start_node is not None:
            strategy_steps.append("toc_anchor_start")
            confidence = 0.85

    if start_node is None:
        start_node = _find_body_heading_node(
            soup=soup,
            spec=spec,
            excluded_table_ids=toc_table_ids,
        )
        if start_node is not None:
            strategy_steps.append("heading_fallback_start")
            if start_anchor is None:
                start_anchor = _node_anchor(start_node)
            confidence = max(confidence, 0.7)

    content_blocks = _collect_content_blocks(soup, excluded_table_ids=toc_table_ids)
    if not content_blocks:
        return CDAExtractionResult(
            section_name=section_name,
            section_key=section_key,
            section_found=False,
            markdown="",
            start_anchor=start_anchor,
            end_anchor=None,
            start_page=start_page,
            end_page=None,
            strategy="no_content_blocks",
            warnings=["No content blocks found in filing body."],
            confidence=0.0,
        )

    start_index = _find_block_index_for_node(content_blocks, start_node)
    if start_index is None:
        start_index = _find_first_section_block(content_blocks, spec)
        strategy_steps.append("keyword_window_start")
        confidence = max(confidence, 0.6)
        if start_index is not None and start_anchor is None:
            start_anchor = _node_anchor(content_blocks[start_index])
    if start_index is None:
        return CDAExtractionResult(
            section_name=section_name,
            section_key=section_key,
            section_found=False,
            markdown="",
            start_anchor=start_anchor,
            end_anchor=None,
            start_page=start_page,
            end_page=None,
            strategy="start_not_found",
            warnings=[f"Start boundary for '{section_name}' could not be resolved."],
            confidence=0.0,
        )

    end_entry = _resolve_end_toc_entry(toc_entries, start_entry, spec)
    end_anchor = end_entry.href.lstrip("#") if (end_entry and end_entry.href) else None
    end_page = end_entry.page if end_entry is not None else None
    end_node = _resolve_anchor_node(soup, end_entry.href) if end_entry is not None else None
    end_index: int | None = None

    if end_node is not None:
        end_index = _find_block_index_for_node(content_blocks, end_node)
        if end_index is not None and end_index > start_index:
            if spec.end_mode == "major_non_exec":
                strategy_steps.append("toc_major_item_end")
            else:
                strategy_steps.append("toc_next_entry_end")
            confidence = max(confidence, 0.92)
        else:
            end_index = None

    if end_index is None:
        end_index = _find_heading_end_index(content_blocks, start_index, spec)
        if end_index is not None:
            if end_anchor is None:
                end_anchor = _node_anchor(content_blocks[end_index])
            if spec.end_mode == "major_non_exec":
                strategy_steps.append("heading_outside_exec_end")
            else:
                strategy_steps.append("heading_next_boundary_end")
            confidence = max(confidence, 0.84)

    if end_index is None and end_page is not None:
        end_index = _find_page_cutoff_index(content_blocks, start_index, end_page)
        if end_index is not None:
            strategy_steps.append("toc_page_cutoff_end")
            confidence = max(confidence, 0.74)

    if end_index is None or end_index <= start_index:
        end_index = len(content_blocks)
        strategy_steps.append("open_end_to_document_tail")
        extraction_warnings.append(
            f"Unable to resolve explicit end boundary for '{section_name}'; using document tail."
        )
        confidence = min(confidence, 0.65)

    selected_blocks = content_blocks[start_index:end_index]
    word_count = _word_count_from_blocks(selected_blocks)
    if word_count < spec.short_word_guard:
        expanded_index = _expand_short_selection(content_blocks, start_index, end_index, end_page)
        if expanded_index > end_index:
            end_index = expanded_index
            selected_blocks = content_blocks[start_index:end_index]
            strategy_steps.append("short_text_auto_expand")
            extraction_warnings.append(
                f"Section '{section_name}' was short ({word_count} words); expanded boundary automatically."
            )
            confidence = max(0.45, confidence - 0.1)

    fragment_html = "".join(str(tag) for tag in selected_blocks)
    markdown: str | None = None
    docling_warning: str | None = None

    if fragment_html.strip():
        markdown, docling_warning = _render_with_docling(fragment_html)
    if docling_warning:
        extraction_warnings.append(docling_warning)

    if not markdown:
        markdown = _render_blocks_to_markdown(selected_blocks)
        strategy_steps.append("fallback_html_to_markdown")
        confidence = max(0.4, min(confidence, 0.8))
    else:
        strategy_steps.append("docling_markdown")

    normalized_markdown = _normalize_markdown(markdown)
    if not normalized_markdown:
        extraction_warnings.append("Rendered markdown is empty after normalization.")
        confidence = min(confidence, 0.2)

    strategy = " -> ".join(strategy_steps) if strategy_steps else "unknown_strategy"
    return CDAExtractionResult(
        section_name=section_name,
        section_key=section_key,
        section_found=bool(normalized_markdown),
        markdown=normalized_markdown,
        start_anchor=start_anchor,
        end_anchor=end_anchor,
        start_page=start_page,
        end_page=end_page,
        strategy=strategy,
        warnings=extraction_warnings,
        confidence=round(confidence, 3),
    )


def extract_cda_markdown(
    raw_html: str,
    metadata: DocumentMetadata | None = None,
) -> CDAExtractionResult:
    """Backward-compatible wrapper for CD&A extraction."""
    return extract_section_markdown(
        raw_html=raw_html,
        metadata=metadata,
        section_name="compensation_discussion_and_analysis",
    )


def _get_section_spec(section_name: str) -> SectionSpec:
    spec = SECTION_SPECS.get(section_name)
    if spec is None:
        supported = ", ".join(SECTION_NAMES)
        msg = f"Unsupported section_name '{section_name}'. Supported values: {supported}"
        raise ValueError(msg)
    return spec


def _build_section_key(metadata: DocumentMetadata | None, section_name: str) -> str | None:
    if metadata is None:
        return None
    fiscal_year = _infer_fiscal_year_from_filing_date(metadata.filing_date)
    return f"{metadata.cik}-{fiscal_year}-{section_name}"


def _infer_fiscal_year_from_filing_date(filing_date: date) -> int:
    return filing_date.year - 1 if filing_date.month <= 8 else filing_date.year


def _build_front_matter_toc(soup: BeautifulSoup) -> tuple[list[_TocEntry], set[int]]:
    entries: list[_TocEntry] = []
    toc_table_ids: set[int] = set()
    tables = soup.find_all("table")
    for table_order, table in enumerate(tables):
        if table_order > _FRONT_MATTER_TABLE_LIMIT:
            break
        rows = table.find_all("tr")
        if len(rows) < 4:
            continue
        parsed_rows: list[_TocEntry] = []
        for row_order, row in enumerate(rows):
            cells = row.find_all(["td", "th"])
            if not cells:
                continue
            row_text = " ".join(cell.get_text(" ", strip=True) for cell in cells)
            page_match = _PAGE_TAIL_PATTERN.search(row_text)
            if page_match is None:
                continue
            raw_label = row_text[: page_match.start()].strip()
            if not raw_label:
                continue
            href = None
            first_link = row.find("a", href=True)
            if isinstance(first_link, Tag):
                href_value = str(first_link.get("href", "")).strip()
                href = href_value if href_value else None
            parsed_rows.append(
                _TocEntry(
                    label=raw_label,
                    normalized_label=_normalize_label(raw_label),
                    page=int(page_match.group(1)),
                    href=href,
                    table_order=table_order,
                    row_order=row_order,
                )
            )
        if not _is_toc_like(parsed_rows):
            continue
        toc_table_ids.add(id(table))
        entries.extend(parsed_rows)

    entries.sort(key=lambda e: (e.table_order, e.row_order))
    return entries, toc_table_ids


def _is_toc_like(rows: list[_TocEntry]) -> bool:
    if len(rows) < 4:
        return False
    href_count = sum(1 for row in rows if row.href and row.href.startswith("#"))
    keyword_count = sum(
        1
        for row in rows
        if any(
            token in row.normalized_label
            for token in (
                "TABLE OF CONTENTS",
                "ITEM ",
                "SECTION ",
                "COMPENSATION",
                "GOVERNANCE",
                "DIRECTOR",
                "OWNERSHIP",
                "PROPOSAL",
            )
        )
    )
    non_decreasing_pairs = sum(1 for i in range(len(rows) - 1) if rows[i].page <= rows[i + 1].page)
    monotonic_ratio = non_decreasing_pairs / max(1, len(rows) - 1)
    if monotonic_ratio < 0.75 and keyword_count < 8:
        return False
    return href_count >= 3 or keyword_count >= 4


def _resolve_section_toc_entry(entries: list[_TocEntry], spec: SectionSpec) -> _TocEntry | None:
    best_score = 0.0
    best_entry: _TocEntry | None = None
    for entry in entries:
        score = _section_label_score(entry.normalized_label, spec)
        if score > best_score:
            best_score = score
            best_entry = entry
    if best_score < spec.min_start_score:
        return None
    return best_entry


def _section_label_score(normalized_label: str, spec: SectionSpec) -> float:
    if not normalized_label:
        return 0.0

    normalized_aliases = [_normalize_label(alias) for alias in spec.aliases]
    tokens = set(normalized_label.split())
    score = 0.0

    for normalized_alias in normalized_aliases:
        if not normalized_alias:
            continue
        ratio = SequenceMatcher(None, normalized_label, normalized_alias).ratio()
        compact_ratio = SequenceMatcher(
            None,
            normalized_label.replace(" ", ""),
            normalized_alias.replace(" ", ""),
        ).ratio()
        alias_tokens = set(normalized_alias.split())
        overlap = len(tokens.intersection(alias_tokens))
        local_score = max(ratio, compact_ratio)
        if normalized_alias in normalized_label:
            local_score = max(local_score, 0.93)
        local_score += 0.05 * min(overlap, 4)
        score = max(score, min(local_score, 1.0))

    required_hits = 0
    for token in spec.required_tokens:
        token_upper = token.upper()
        if token_upper in tokens or token_upper in normalized_label:
            required_hits += 1

    if spec.section_name == "compensation_discussion_and_analysis":
        has_discussion_analysis = "DISCUSSION" in tokens and "ANALYSIS" in tokens
        has_cda_abbrev = "CD&A" in normalized_label or "CDA" in tokens
        overlap = len(tokens.intersection({"COMPENSATION", "DISCUSSION", "ANALYSIS"}))
        score += 0.12 * overlap
        if has_discussion_analysis:
            score += 0.16
        if has_cda_abbrev:
            score += 0.12
        if not has_discussion_analysis and not has_cda_abbrev:
            score *= 0.45

    if required_hits < spec.min_required_token_hits:
        score *= 0.4

    return min(score, 1.0)


def _resolve_end_toc_entry(
    entries: list[_TocEntry],
    start_entry: _TocEntry | None,
    spec: SectionSpec,
) -> _TocEntry | None:
    if start_entry is None:
        return None
    start_key = (start_entry.table_order, start_entry.row_order)

    if spec.end_mode == "major_non_exec":
        candidates = [
            entry
            for entry in entries
            if (entry.table_order, entry.row_order) > start_key and _is_major_non_exec_label(entry)
        ]
        return candidates[0] if candidates else None

    for entry in entries:
        if (entry.table_order, entry.row_order) <= start_key:
            continue
        if entry.normalized_label == start_entry.normalized_label:
            continue
        return entry
    return None


def _is_major_non_exec_label(entry: _TocEntry) -> bool:
    label = entry.normalized_label
    item_match = _ITEM_PATTERN.match(label)
    if item_match:
        try:
            item_number = int(item_match.group(1))
        except ValueError:
            item_number = 0
        if item_number >= 4:
            return True
        if item_number == 3 and "SAY-ON-PAY" not in label and "EXECUTIVE COMPENSATION" not in label:
            return True

    if any(term in label for term in _NON_EXEC_TERMS):
        return True
    if any(term in label for term in _EXEC_COMP_TERMS):
        return False
    # Rows outside known compensation family are treated as boundary candidates.
    return True


def _resolve_anchor_node(soup: BeautifulSoup, href: str | None) -> Tag | None:
    if not href:
        return None
    anchor = href.strip()
    if not anchor:
        return None
    target = anchor.split("#", 1)[1] if "#" in anchor else anchor
    target = target.strip()
    if not target:
        return None

    node = soup.find(id=target)
    if isinstance(node, Tag):
        return node
    node = soup.find(attrs={"name": target})
    if isinstance(node, Tag):
        return node
    return None


def _find_body_heading_node(
    soup: BeautifulSoup,
    spec: SectionSpec,
    excluded_table_ids: set[int],
) -> Tag | None:
    best: tuple[float, Tag] | None = None
    for tag in soup.find_all(_SEARCH_TAGS):
        if _is_in_excluded_table(tag, excluded_table_ids):
            continue
        if tag.name == "table":
            continue
        text = _normalize_label(tag.get_text(" ", strip=True))
        if not text or text == "TABLE OF CONTENTS":
            continue
        if len(text) > 220:
            continue
        score = _section_label_score(text, spec)
        if score < spec.min_heading_score:
            continue
        if best is None or score > best[0]:
            best = (score, tag)
    return best[1] if best else None


def _collect_content_blocks(soup: BeautifulSoup, excluded_table_ids: set[int]) -> list[Tag]:
    blocks: list[Tag] = []
    for tag in soup.find_all(_CONTENT_BLOCK_TAGS):
        if _is_in_excluded_table(tag, excluded_table_ids):
            continue
        if tag.name in _LIST_TAGS and tag.find_parent(_LIST_TAGS):
            continue
        if tag.name == "p" and not _normalize_text(tag.get_text(" ", strip=True)):
            continue
        blocks.append(tag)
    return blocks


def _is_in_excluded_table(tag: Tag, excluded_table_ids: set[int]) -> bool:
    parent_table = tag.find_parent("table")
    if parent_table is None:
        return False
    return id(parent_table) in excluded_table_ids


def _find_block_index_for_node(blocks: list[Tag], node: Tag | None) -> int | None:
    if node is None:
        return None
    for index, block in enumerate(blocks):
        if block is node:
            return index
        if node in block.descendants:
            return index
    return None


def _find_first_section_block(blocks: list[Tag], spec: SectionSpec) -> int | None:
    for index, block in enumerate(blocks):
        text = _normalize_label(block.get_text(" ", strip=True))
        if not text or text == "TABLE OF CONTENTS":
            continue
        score = _section_label_score(text, spec)
        if score >= spec.min_keyword_score:
            return index
    return None


def _find_heading_end_index(blocks: list[Tag], start_index: int, spec: SectionSpec) -> int | None:
    for index in range(start_index + 1, len(blocks)):
        block = blocks[index]
        text = _normalize_label(block.get_text(" ", strip=True))
        if not text:
            continue
        if not _looks_like_heading(block, text):
            continue

        if spec.end_mode == "major_non_exec":
            if _is_exec_comp_heading(text):
                continue
            if _is_major_heading_boundary(text):
                return index
            continue

        if text == "TABLE OF CONTENTS":
            continue
        if _section_label_score(text, spec) >= max(0.72, spec.min_heading_score):
            continue
        return index
    return None


def _looks_like_heading(block: Tag, normalized_text: str) -> bool:
    if block.name in _HEADING_TAGS:
        return True
    if len(normalized_text) == 0 or len(normalized_text) > 180:
        return False
    alpha = re.sub(r"[^A-Z]+", "", normalized_text)
    if not alpha:
        return False
    if alpha.isupper() and len(alpha) >= 10:
        return True
    if normalized_text.startswith("ITEM "):
        return True
    return normalized_text.startswith("SECTION ")


def _is_exec_comp_heading(normalized_text: str) -> bool:
    return any(term in normalized_text for term in _EXEC_COMP_TERMS)


def _is_major_heading_boundary(normalized_text: str) -> bool:
    item_match = _ITEM_PATTERN.match(normalized_text)
    if item_match:
        try:
            return int(item_match.group(1)) >= 4
        except ValueError:
            return False
    return any(term in normalized_text for term in _NON_EXEC_TERMS)


def _find_page_cutoff_index(blocks: list[Tag], start_index: int, end_page: int) -> int | None:
    for index in range(start_index + 1, len(blocks)):
        page_value = _extract_page_hint(blocks[index].get_text(" ", strip=True))
        if page_value is None:
            continue
        if page_value >= end_page:
            return index
    return None


def _extract_page_hint(text: str) -> int | None:
    compact = " ".join(text.split())
    patterns = (
        re.compile(r"^[•·\-–—]?\s*(\d{1,3})\s+20\d{2}\s+PROXY STATEMENT", re.IGNORECASE),
        re.compile(r"^PAGE\s+(\d{1,3})(?:\s+OF\s+\d{1,3})?$", re.IGNORECASE),
        re.compile(r"^(\d{1,3})$"),
    )
    for pattern in patterns:
        match = pattern.search(compact)
        if match is not None:
            try:
                return int(match.group(1))
            except ValueError:
                return None
    return None


def _expand_short_selection(
    blocks: list[Tag],
    start_index: int,
    end_index: int,
    end_page: int | None,
) -> int:
    if end_index >= len(blocks):
        return end_index
    # Prefer extending to the next page transition if page hints exist.
    if end_page is not None:
        for index in range(end_index + 1, len(blocks)):
            page_value = _extract_page_hint(blocks[index].get_text(" ", strip=True))
            if page_value is not None and page_value >= (end_page + 2):
                return index
    return min(len(blocks), max(end_index + 80, start_index + 120))


def _word_count_from_blocks(blocks: list[Tag]) -> int:
    total = 0
    for block in blocks:
        total += len(_normalize_text(block.get_text(" ", strip=True)).split())
    return total


def _render_with_docling(fragment_html: str) -> tuple[str | None, str | None]:
    try:
        from docling.document_converter import DocumentConverter
        from docling.datamodel.base_models import InputFormat
    except Exception as exc:  # noqa: BLE001
        return None, f"Docling unavailable, used fallback renderer ({exc.__class__.__name__})."

    converter = DocumentConverter()
    result: Any = None

    if hasattr(converter, "convert_string"):
        try:
            result = converter.convert_string(
                fragment_html,
                format=InputFormat.HTML,
                name="section_fragment",
            )
        except Exception:  # noqa: BLE001
            result = None

    if result is None:
        tmp_path = ""
        try:
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                suffix=".html",
                delete=False,
            ) as handle:
                handle.write(fragment_html)
                tmp_path = handle.name
            result = converter.convert(tmp_path)
        except Exception as exc:  # noqa: BLE001
            return None, f"Docling conversion failed, used fallback renderer ({exc.__class__.__name__})."
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    document = getattr(result, "document", None)
    if document is None:
        document = result
    markdown = _extract_markdown_from_docling(document)
    if markdown is None:
        return None, "Docling result did not expose markdown export; used fallback renderer."
    return markdown, None


def _extract_markdown_from_docling(document: Any) -> str | None:
    for method_name in ("export_to_markdown", "to_markdown"):
        method = getattr(document, method_name, None)
        if not callable(method):
            continue
        try:
            result = method()
        except Exception:  # noqa: BLE001
            continue
        if isinstance(result, str):
            return result
    markdown_attr = getattr(document, "markdown", None)
    if isinstance(markdown_attr, str):
        return markdown_attr
    return None


def _render_blocks_to_markdown(blocks: list[Tag]) -> str:
    lines: list[str] = []
    for block in blocks:
        if block.name in _HEADING_TAGS:
            level = int(block.name[1])
            text = _normalize_text(block.get_text(" ", strip=True))
            if text:
                lines.extend([f"{'#' * level} {text}", ""])
            continue

        if block.name == "table":
            table_lines = _table_to_markdown(block)
            if table_lines:
                lines.extend(table_lines)
                lines.append("")
            continue

        if block.name in _LIST_TAGS:
            list_lines = _list_to_markdown(block)
            if list_lines:
                lines.extend(list_lines)
                lines.append("")
            continue

        text = _normalize_text(block.get_text(" ", strip=True))
        if text:
            lines.extend([text, ""])

    return "\n".join(lines).strip()


def _table_to_markdown(table: Tag) -> list[str]:
    rows: list[list[str]] = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if not cells:
            continue
        row = [_escape_cell(_normalize_text(cell.get_text(" ", strip=True))) for cell in cells]
        if any(cell for cell in row):
            rows.append(row)

    if not rows:
        return []

    width = max(len(row) for row in rows)
    normalized_rows = [row + [""] * (width - len(row)) for row in rows]
    header = normalized_rows[0]
    separator = ["---"] * width

    output = [
        f"| {' | '.join(header)} |",
        f"| {' | '.join(separator)} |",
    ]
    for row in normalized_rows[1:]:
        output.append(f"| {' | '.join(row)} |")
    return output


def _list_to_markdown(tag: Tag) -> list[str]:
    lines: list[str] = []
    ordered = tag.name == "ol"
    item_number = 1
    for item in tag.find_all("li", recursive=False):
        text = _normalize_text(item.get_text(" ", strip=True))
        if not text:
            continue
        prefix = f"{item_number}. " if ordered else "- "
        lines.append(prefix + text)
        if ordered:
            item_number += 1
    return lines


def _escape_cell(value: str) -> str:
    return value.replace("|", r"\|")


def _normalize_markdown(markdown: str) -> str:
    text = markdown.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _normalize_label(text: str) -> str:
    compact = _normalize_text(text).upper()
    compact = re.sub(r"[^A-Z0-9\s:&/\-]", " ", compact)
    compact = re.sub(r"\s+", " ", compact).strip()
    return compact


def _normalize_text(text: str) -> str:
    return " ".join(text.replace("\xa0", " ").split())


def _node_anchor(node: Tag) -> str | None:
    anchor_id = node.get("id")
    if isinstance(anchor_id, str) and anchor_id.strip():
        return anchor_id.strip()
    anchor_name = node.get("name")
    if isinstance(anchor_name, str) and anchor_name.strip():
        return anchor_name.strip()
    return None
