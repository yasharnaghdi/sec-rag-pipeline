"""DEF 14A section extractor using DOM-linear-walk strategy.

This module replaces ``cda_markdown_extractor`` with a from-scratch approach
that walks the DOM directly from anchor to anchor rather than pre-collecting
content blocks and mapping indices.  The DOM walk handles three filing
structural families:

* **Family A** (Donnelley/Edgar): standard TOC with ``#tocXXX_N`` anchors,
  headings may use ``<h1>``–``<h6>``.
* **Family B** (Workiva): empty ``<div id>`` anchors, all content in
  ``<div>`` tags, no heading tags.
* **Family C** (no-link TOC): TOC table with page numbers but no ``href``
  links; headings are bold ``<p>`` or ``<b>`` text.
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
import warnings
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, Literal

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from bs4.element import Tag
from pydantic import BaseModel, Field

from ingestion.metadata_model import DocumentMetadata

log = logging.getLogger(__name__)

# ── regex patterns ──────────────────────────────────────────────────────────

_PAGE_TAIL_RE = re.compile(r"\b(\d{1,3})\s*$")
_PAGE_LEAD_RE = re.compile(r"^\s*(\d{1,3})\s+")
_PAGE_ONLY_RE = re.compile(r"^\s*(\d{1,3})\s*$")
_ITEM_RE = re.compile(r"^ITEM\s+(\d+)\b", re.IGNORECASE)
_FONT_SIZE_RE = re.compile(r"font-size\s*:\s*([\d.]+)\s*pt", re.IGNORECASE)
_TOC_BACKLINK_RE = re.compile(r"^\s*table\s+of\s+contents\s*$", re.IGNORECASE)
_PAGE_FOOTER_RE = re.compile(
    r"^\s*[-\u2013\u2014\u2022]?\s*\d{1,3}\s+(20\d{2}\s+)?PROXY\s+STATEMENT"
    r"|^\s*20\d{2}\s+PROXY\s+STATEMENT\s+\d{1,3}\s*[-\u2013\u2014\u2022]?\s*$"
    r"|^\s*PAGE\s+\d{1,3}(\s+OF\s+\d{1,3})?\s*$"
    r"|^\s*-?\s*\d{1,3}\s*-?\s*$",
    re.IGNORECASE,
)

_HEADING_TAGS: tuple[str, ...] = ("h1", "h2", "h3", "h4", "h5", "h6")
_LIST_TAGS: tuple[str, ...] = ("ul", "ol")
_FRONT_MATTER_TABLE_LIMIT = 80
_MIN_WORDS_GUARD = 120
_MAX_ANCHOR_HOPS = 25

# ── terms for boundary classification ───────────────────────────────────────

_EXEC_COMP_TERMS = (
    "EXECUTIVE COMPENSATION",
    "COMPENSATION DISCUSSION",
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


# ══════════════════════════════════════════════════════════════════════════════
#  Data structures
# ══════════════════════════════════════════════════════════════════════════════


@dataclass(frozen=True)
class TocEntry:
    """One row from the filing's table of contents."""

    label: str
    normalized: str
    page: int
    href: str | None
    table_idx: int
    row_idx: int


@dataclass(frozen=True)
class SectionSpec:
    """Definition of one target section to extract."""

    section_name: str
    aliases: tuple[str, ...]
    end_mode: Literal["major_non_exec", "next_boundary"]
    required_tokens: tuple[str, ...] = ()
    min_required_hits: int = 0
    min_match_score: float = 0.62


class SectionExtractionResult(BaseModel):
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


# backward-compat alias
CDAExtractionResult = SectionExtractionResult


# ── section specs ───────────────────────────────────────────────────────────

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
        required_tokens=("COMPENSATION", "DISCUSSION", "ANALYSIS", "CD&A", "CDA"),
        min_required_hits=2,
    ),
    SectionSpec(
        section_name="executive_compensation",
        aliases=(
            "EXECUTIVE COMPENSATION",
            "EXECUTIVE COMPENSATION DISCUSSION",
        ),
        end_mode="major_non_exec",
        required_tokens=("EXECUTIVE", "COMPENSATION"),
        min_required_hits=2,
        min_match_score=0.64,
    ),
    SectionSpec(
        section_name="director_compensation",
        aliases=(
            "DIRECTOR COMPENSATION",
            "COMPENSATION OF DIRECTORS",
        ),
        end_mode="next_boundary",
        required_tokens=("DIRECTOR", "COMPENSATION"),
        min_required_hits=2,
        min_match_score=0.64,
    ),
    SectionSpec(
        section_name="pay_vs_performance",
        aliases=(
            "PAY VERSUS PERFORMANCE",
            "PAY VS PERFORMANCE",
        ),
        end_mode="next_boundary",
        required_tokens=("PAY", "PERFORMANCE"),
        min_required_hits=2,
    ),
    SectionSpec(
        section_name="equity_compensation_plans",
        aliases=(
            "EQUITY COMPENSATION PLANS",
            "EQUITY COMPENSATION PLAN",
        ),
        end_mode="next_boundary",
        required_tokens=("EQUITY", "COMPENSATION", "PLAN", "PLANS"),
        min_required_hits=2,
    ),
)

SECTION_SPECS: dict[str, SectionSpec] = {s.section_name: s for s in _SECTION_SPECS}
SECTION_NAMES: tuple[str, ...] = tuple(s.section_name for s in _SECTION_SPECS)


# ══════════════════════════════════════════════════════════════════════════════
#  Text helpers
# ══════════════════════════════════════════════════════════════════════════════


def _normalize_text(text: str) -> str:
    return " ".join(text.replace("\xa0", " ").split())


def _normalize_label(text: str) -> str:
    compact = _normalize_text(text).upper()
    compact = re.sub(r"[^A-Z0-9\s:&/\-]", " ", compact)
    return re.sub(r"\s+", " ", compact).strip()


def _extract_font_size_pt(style: str | None) -> float | None:
    if not style:
        return None
    m = _FONT_SIZE_RE.search(style)
    return float(m.group(1)) if m else None


# ══════════════════════════════════════════════════════════════════════════════
#  TOC parsing  (Section B)
# ══════════════════════════════════════════════════════════════════════════════


def _parse_toc_tables(soup: BeautifulSoup) -> tuple[list[TocEntry], set[int]]:
    """Extract TOC entries from front-matter tables.

    Handles both trailing page numbers (``Label  5``) and leading page
    numbers (``5  Label``) by analyzing individual cells.

    Returns (ordered entries, python-id set of TOC table elements).
    """
    entries: list[TocEntry] = []
    toc_table_ids: set[int] = set()

    for table_idx, table in enumerate(soup.find_all("table")):
        if table_idx > _FRONT_MATTER_TABLE_LIMIT:
            break
        rows = table.find_all("tr")
        if len(rows) < 4:
            continue

        parsed: list[TocEntry] = []
        for row_idx, row in enumerate(rows):
            entry = _parse_toc_row(row, table_idx, row_idx)
            if entry is not None:
                parsed.append(entry)

        if not _is_toc_like(parsed):
            continue
        toc_table_ids.add(id(table))
        entries.extend(parsed)

    entries.sort(key=lambda e: (e.table_idx, e.row_idx))
    return entries, toc_table_ids


def _parse_toc_row(row: Tag, table_idx: int, row_idx: int) -> TocEntry | None:
    """Parse a single TOC table row, handling both leading and trailing page numbers."""
    cells = row.find_all(["td", "th"])
    if not cells:
        return None

    # Extract cell texts
    cell_texts = [c.get_text(" ", strip=True) for c in cells]

    # Strategy 1: find a cell that is ONLY a page number (1-3 digits)
    page_num: int | None = None
    label_parts: list[str] = []
    for ct in cell_texts:
        if not ct:
            continue
        if _PAGE_ONLY_RE.match(ct) and page_num is None:
            page_num = int(ct.strip())
        else:
            label_parts.append(ct)

    raw_label = " ".join(label_parts).strip()

    # Strategy 2: if no page-only cell, try trailing page number on joined text
    if page_num is None:
        row_text = " ".join(ct for ct in cell_texts if ct)
        tail_m = _PAGE_TAIL_RE.search(row_text)
        if tail_m:
            page_num = int(tail_m.group(1))
            raw_label = row_text[: tail_m.start()].strip()

    # Strategy 3: try leading page number on joined text
    if page_num is None:
        row_text = " ".join(ct for ct in cell_texts if ct)
        lead_m = _PAGE_LEAD_RE.match(row_text)
        if lead_m:
            page_num = int(lead_m.group(1))
            raw_label = row_text[lead_m.end() :].strip()

    if page_num is None or not raw_label:
        return None

    # Extract href from first link in the row
    href: str | None = None
    first_link = row.find("a", href=True)
    if isinstance(first_link, Tag):
        href_val = str(first_link.get("href", "")).strip()
        href = href_val or None

    return TocEntry(
        label=raw_label,
        normalized=_normalize_label(raw_label),
        page=page_num,
        href=href,
        table_idx=table_idx,
        row_idx=row_idx,
    )


def _is_toc_like(rows: list[TocEntry]) -> bool:
    if len(rows) < 4:
        return False
    href_count = sum(1 for r in rows if r.href and r.href.startswith("#"))
    kw_count = sum(
        1
        for r in rows
        if any(
            tok in r.normalized
            for tok in (
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
    non_dec_pairs = sum(1 for i in range(len(rows) - 1) if rows[i].page <= rows[i + 1].page)
    mono_ratio = non_dec_pairs / max(1, len(rows) - 1)
    if mono_ratio < 0.75 and kw_count < 8:
        return False
    return href_count >= 3 or kw_count >= 4


# ── section matching in TOC ─────────────────────────────────────────────────


def _section_label_score(normalized_label: str, spec: SectionSpec) -> float:
    """Score how well *normalized_label* matches *spec*."""
    if not normalized_label:
        return 0.0

    tokens = set(normalized_label.split())
    score = 0.0

    for alias in spec.aliases:
        norm_alias = _normalize_label(alias)
        if not norm_alias:
            continue
        ratio = SequenceMatcher(None, normalized_label, norm_alias).ratio()
        compact_ratio = SequenceMatcher(
            None,
            normalized_label.replace(" ", ""),
            norm_alias.replace(" ", ""),
        ).ratio()
        alias_tokens = set(norm_alias.split())
        overlap = len(tokens & alias_tokens)
        local = max(ratio, compact_ratio)
        if norm_alias in normalized_label:
            local = max(local, 0.93)
        local += 0.05 * min(overlap, 4)
        score = max(score, min(local, 1.0))

    # CD&A-specific boosting
    if spec.section_name == "compensation_discussion_and_analysis":
        has_disc_anal = "DISCUSSION" in tokens and "ANALYSIS" in tokens
        has_abbrev = "CD&A" in normalized_label or "CDA" in tokens
        overlap = len(tokens & {"COMPENSATION", "DISCUSSION", "ANALYSIS"})
        score += 0.12 * overlap
        if has_disc_anal:
            score += 0.16
        if has_abbrev:
            score += 0.12
        if not has_disc_anal and not has_abbrev:
            score *= 0.45

    # required-tokens penalty
    req_hits = sum(
        1 for t in spec.required_tokens if t.upper() in tokens or t.upper() in normalized_label
    )
    if req_hits < spec.min_required_hits:
        score *= 0.4

    return min(score, 1.0)


def _match_section_in_toc(entries: list[TocEntry], spec: SectionSpec) -> TocEntry | None:
    best_score = 0.0
    best: TocEntry | None = None
    for entry in entries:
        s = _section_label_score(entry.normalized, spec)
        if s > best_score:
            best_score = s
            best = entry
    return best if best_score >= spec.min_match_score else None


def _find_end_toc_entry(
    entries: list[TocEntry],
    start_entry: TocEntry | None,
    spec: SectionSpec,
) -> TocEntry | None:
    if start_entry is None:
        return None
    start_key = (start_entry.table_idx, start_entry.row_idx)

    if spec.end_mode == "major_non_exec":
        for entry in entries:
            if (entry.table_idx, entry.row_idx) <= start_key:
                continue
            if _is_major_non_exec_label(entry):
                return entry
        return None

    # next_boundary: return the very next distinct entry
    for entry in entries:
        if (entry.table_idx, entry.row_idx) <= start_key:
            continue
        if entry.normalized == start_entry.normalized:
            continue
        return entry
    return None


def _is_major_non_exec_label(entry: TocEntry) -> bool:
    label = entry.normalized
    item_m = _ITEM_RE.match(label)
    if item_m:
        try:
            num = int(item_m.group(1))
        except ValueError:
            num = 0
        if num >= 4:
            return True
        if num == 3 and "SAY-ON-PAY" not in label and "EXECUTIVE COMPENSATION" not in label:
            return True
    if any(term in label for term in _NON_EXEC_TERMS):
        return True
    if any(term in label for term in _EXEC_COMP_TERMS):
        return False
    return True


# ══════════════════════════════════════════════════════════════════════════════
#  Anchor resolution  (Section C)
# ══════════════════════════════════════════════════════════════════════════════


def _resolve_anchor_target(soup: BeautifulSoup, href: str | None) -> Tag | None:
    """Resolve a TOC href (``#anchor``) to the target DOM element."""
    if not href:
        return None
    target = href.split("#", 1)[1] if "#" in href else href
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


def _find_content_start_from_anchor(
    anchor_node: Tag,
    spec: SectionSpec,
) -> Tag | None:
    """Walk forward from *anchor_node* past noise to find the content start.

    Handles Family B filings where the anchor is an empty ``<div>``
    several siblings away from actual content.

    Returns the first content-bearing node (heading or body text).
    """
    # case 1: anchor itself has matching text
    text = anchor_node.get_text(" ", strip=True)
    if text and len(text) < 200:
        norm = _normalize_label(text)
        if norm and _section_label_score(norm, spec) >= spec.min_match_score:
            return anchor_node

    # case 2: walk forward
    current: Tag | None = anchor_node
    hops = 0
    while hops < _MAX_ANCHOR_HOPS:
        current = _next_element_sibling(current)
        if current is None:
            break
        hops += 1
        if _is_page_break_noise(current):
            continue
        text = current.get_text(" ", strip=True)
        if not text or len(text.strip()) < 3:
            continue

        norm = _normalize_label(text)
        if norm and _section_label_score(norm, spec) >= spec.min_match_score:
            return current

        # walked past noise and found real content – use it
        if hops >= 3:
            return current

    return None


# ══════════════════════════════════════════════════════════════════════════════
#  Visual heading detection  (Section D)
# ══════════════════════════════════════════════════════════════════════════════


_BLOCK_LEVEL_TAGS: tuple[str, ...] = (
    "p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article",
)


def _is_visual_heading(tag: Tag) -> bool:
    """Return True if *tag* looks like a section heading visually.

    Only considers block-level elements (``p``, ``div``, ``h1``–``h6``) to
    avoid false positives from inline ``<font>``, ``<b>``, ``<td>`` etc.
    """
    if tag.name not in _BLOCK_LEVEL_TAGS and tag.name not in _HEADING_TAGS:
        return False

    text = tag.get_text(" ", strip=True)
    if not text or len(text) > 180:
        return False

    # explicit heading tags
    if tag.name in _HEADING_TAGS:
        return True

    style = tag.get("style", "") or ""
    if isinstance(style, list):
        style = " ".join(style)

    # large font-size
    fs = _extract_font_size_pt(style)
    if fs is not None and fs >= 14:
        return True

    # all-caps with significant alpha content
    alpha_only = re.sub(r"[^A-Za-z]", "", text)
    if len(alpha_only) >= 10 and alpha_only == alpha_only.upper():
        return True

    # bold + centered
    style_compact = style.replace(" ", "").lower()
    has_bold = (
        tag.find("b") is not None
        or tag.find("strong") is not None
        or "font-weight:bold" in style_compact
        or "font-weight:700" in style_compact
    )
    is_centered = "text-align:center" in style_compact
    if has_bold and is_centered and len(text) < 100:
        return True

    # bold + underlined
    has_underline = "text-decoration:underline" in style_compact or tag.find("u") is not None
    if has_bold and has_underline and len(text) < 100:
        return True

    return False


def _find_heading_in_body(
    soup: BeautifulSoup,
    spec: SectionSpec,
    toc_table_ids: set[int],
) -> Tag | None:
    """Fallback: scan body for a block-level visual heading matching *spec*."""
    best: tuple[float, Tag] | None = None
    # Only search block-level + heading tags to avoid <td>, <font>, <b> false positives
    search_tags = list(_BLOCK_LEVEL_TAGS) + list(_HEADING_TAGS)
    for tag in soup.find_all(search_tags):
        if _is_in_toc_table(tag, toc_table_ids):
            continue
        text = tag.get_text(" ", strip=True)
        if not text or len(text) > 220:
            continue
        norm = _normalize_label(text)
        if not norm or norm == "TABLE OF CONTENTS":
            continue
        if not _is_visual_heading(tag):
            continue
        score = _section_label_score(norm, spec)
        if score < spec.min_match_score:
            continue
        if best is None or score > best[0]:
            best = (score, tag)
    return best[1] if best else None


def _is_in_toc_table(tag: Tag, toc_table_ids: set[int]) -> bool:
    if tag.name == "table" and id(tag) in toc_table_ids:
        return True
    parent = tag.find_parent("table")
    return parent is not None and id(parent) in toc_table_ids


# ══════════════════════════════════════════════════════════════════════════════
#  DOM linear walk  (Section E) – core change
# ══════════════════════════════════════════════════════════════════════════════


def _next_element_sibling(node: Tag | None) -> Tag | None:
    """Get the next element sibling, walking up the tree if needed."""
    if node is None:
        return None
    sib = node.next_sibling
    while sib is not None:
        if isinstance(sib, Tag):
            return sib
        sib = sib.next_sibling
    # walk up to parent's next sibling (but stay inside body)
    parent = node.parent
    if parent is not None and parent.name not in ("body", "html", "[document]"):
        return _next_element_sibling(parent)
    return None


def _is_page_break_noise(tag: Tag) -> bool:
    """Return True if *tag* is page-break decoration / noise."""
    if tag.name == "hr":
        return True

    text = tag.get_text(" ", strip=True)

    # "Table of Contents" back-link
    if _TOC_BACKLINK_RE.match(text):
        return True

    # page footer patterns
    if _PAGE_FOOTER_RE.match(text):
        return True

    # empty structural elements (spacers)
    if len(text) == 0:
        return True

    return False


def _is_end_boundary_heading(
    tag: Tag,
    normalized_text: str,
    spec: SectionSpec,
    toc_table_ids: set[int],
) -> bool:
    """Check whether *tag* is a heading marking the end of the section."""
    if not normalized_text:
        return False
    if _is_in_toc_table(tag, toc_table_ids):
        return False
    if not _is_visual_heading(tag):
        return False
    if normalized_text == "TABLE OF CONTENTS":
        return False

    if spec.end_mode == "major_non_exec":
        # stop at non-exec headings but skip exec-comp headings
        if any(term in normalized_text for term in _EXEC_COMP_TERMS):
            return False
        item_m = _ITEM_RE.match(normalized_text)
        if item_m:
            try:
                return int(item_m.group(1)) >= 4
            except ValueError:
                return False
        return any(term in normalized_text for term in _NON_EXEC_TERMS)

    # next_boundary: stop at any heading that matches a DIFFERENT known section
    # or is clearly a different major section
    same_section_score = _section_label_score(normalized_text, spec)
    if same_section_score >= max(0.72, spec.min_match_score):
        return False  # same section label → skip

    # check if it matches any other known section spec
    for other_spec in _SECTION_SPECS:
        if other_spec.section_name == spec.section_name:
            continue
        other_score = _section_label_score(normalized_text, other_spec)
        if other_score >= other_spec.min_match_score:
            return True

    # check for generic boundary markers
    item_m = _ITEM_RE.match(normalized_text)
    if item_m:
        return True
    if any(term in normalized_text for term in _NON_EXEC_TERMS):
        return True

    return False


def _collect_section_nodes(
    start_node: Tag,
    end_node: Tag | None,
    spec: SectionSpec,
    toc_table_ids: set[int],
) -> list[Tag]:
    """Walk DOM siblings from *start_node* to *end_node*, collecting content.

    This is the **core** of the new approach.
    """
    collected: list[Tag] = []
    current: Tag | None = start_node

    while current is not None:
        # end-boundary: hit the resolved end node
        if end_node is not None and (current is end_node or _tag_contains(current, end_node)):
            break

        # skip noise
        if _is_page_break_noise(current):
            current = _next_element_sibling(current)
            continue

        # skip TOC tables
        if current.name == "table" and id(current) in toc_table_ids:
            current = _next_element_sibling(current)
            continue

        # heuristic end-boundary check (always, as safety)
        text = current.get_text(" ", strip=True)
        if text:
            norm = _normalize_label(text)
            if _is_end_boundary_heading(current, norm, spec, toc_table_ids):
                break

        # collect content
        if text and len(text.strip()) > 0:
            collected.append(current)

        current = _next_element_sibling(current)

    return collected


def _tag_contains(outer: Tag, inner: Tag) -> bool:
    """Return True if *inner* is a descendant of *outer*."""
    return inner in outer.descendants


# ══════════════════════════════════════════════════════════════════════════════
#  Markdown rendering  (Section F)
# ══════════════════════════════════════════════════════════════════════════════


def _render_to_markdown(nodes: list[Tag]) -> tuple[str, list[str]]:
    """Convert collected DOM nodes to markdown.

    Returns (markdown_text, list_of_warnings).
    """
    fragment_html = "".join(str(tag) for tag in nodes)
    render_warnings: list[str] = []
    markdown: str | None = None

    if fragment_html.strip():
        markdown, docling_warn = _render_with_docling(fragment_html)
        if docling_warn:
            render_warnings.append(docling_warn)

    if not markdown:
        markdown = _render_fallback(nodes)
        if not any("fallback" in w.lower() for w in render_warnings):
            render_warnings.append("Used fallback HTML-to-markdown renderer.")
    return _normalize_markdown(markdown or ""), render_warnings


def _render_with_docling(fragment_html: str) -> tuple[str | None, str | None]:
    try:
        from docling.document_converter import DocumentConverter
        from docling.datamodel.base_models import InputFormat
    except Exception as exc:  # noqa: BLE001
        return None, f"Docling unavailable ({exc.__class__.__name__})."

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
                "w", encoding="utf-8", suffix=".html", delete=False
            ) as fh:
                fh.write(fragment_html)
                tmp_path = fh.name
            result = converter.convert(tmp_path)
        except Exception as exc:  # noqa: BLE001
            return None, f"Docling conversion failed ({exc.__class__.__name__})."
        finally:
            if tmp_path and os.path.exists(tmp_path):
                os.unlink(tmp_path)

    document = getattr(result, "document", None) or result
    md = _extract_docling_markdown(document)
    if md is None:
        return None, "Docling result did not expose markdown export."
    return md, None


def _extract_docling_markdown(document: Any) -> str | None:
    for method in ("export_to_markdown", "to_markdown"):
        fn = getattr(document, method, None)
        if not callable(fn):
            continue
        try:
            val = fn()
        except Exception:  # noqa: BLE001
            continue
        if isinstance(val, str):
            return val
    attr = getattr(document, "markdown", None)
    return attr if isinstance(attr, str) else None


def _render_fallback(nodes: list[Tag]) -> str:
    """Built-in HTML→markdown renderer (handles ``<div>`` content)."""
    lines: list[str] = []
    for node in nodes:
        if node.name in _HEADING_TAGS:
            level = int(node.name[1])
            text = _normalize_text(node.get_text(" ", strip=True))
            if text:
                lines.extend([f"{'#' * level} {text}", ""])
            continue

        if node.name == "table":
            tbl = _table_to_markdown(node)
            if tbl:
                lines.extend(tbl)
                lines.append("")
            continue

        if node.name in _LIST_TAGS:
            lst = _list_to_markdown(node)
            if lst:
                lines.extend(lst)
                lines.append("")
            continue

        # Handle <p>, <div>, and any other content-bearing elements
        # Check if this element looks like a heading visually
        text = _normalize_text(node.get_text(" ", strip=True))
        if not text:
            continue

        if _is_visual_heading(node) and len(text) < 100:
            lines.extend([f"## {text}", ""])
        else:
            lines.extend([text, ""])

    return "\n".join(lines).strip()


def _table_to_markdown(table: Tag) -> list[str]:
    rows: list[list[str]] = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["th", "td"])
        if not cells:
            continue
        row = [_escape_pipe(_normalize_text(c.get_text(" ", strip=True))) for c in cells]
        if any(cell for cell in row):
            rows.append(row)
    if not rows:
        return []
    width = max(len(r) for r in rows)
    normed = [r + [""] * (width - len(r)) for r in rows]
    header = normed[0]
    sep = ["---"] * width
    out = [f"| {' | '.join(header)} |", f"| {' | '.join(sep)} |"]
    for r in normed[1:]:
        out.append(f"| {' | '.join(r)} |")
    return out


def _list_to_markdown(tag: Tag) -> list[str]:
    lines: list[str] = []
    ordered = tag.name == "ol"
    num = 1
    for item in tag.find_all("li", recursive=False):
        text = _normalize_text(item.get_text(" ", strip=True))
        if not text:
            continue
        prefix = f"{num}. " if ordered else "- "
        lines.append(prefix + text)
        if ordered:
            num += 1
    return lines


def _escape_pipe(val: str) -> str:
    return val.replace("|", r"\|")


def _normalize_markdown(md: str) -> str:
    text = md.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ══════════════════════════════════════════════════════════════════════════════
#  Confidence scoring  (Section G)
# ══════════════════════════════════════════════════════════════════════════════


def _compute_confidence(
    toc_found: bool,
    anchor_resolved: bool,
    content_start_found: bool,
    end_boundary_found: bool,
    strategy_steps: list[str],
    word_count: int,
    docling_succeeded: bool = True,
) -> float:
    if toc_found and anchor_resolved and end_boundary_found:
        base = 0.92
    elif toc_found and anchor_resolved:
        base = 0.80
    elif toc_found and content_start_found:
        base = 0.75
    elif content_start_found:
        base = 0.55
    else:
        base = 0.35

    if word_count < _MIN_WORDS_GUARD:
        base -= 0.10
    if not docling_succeeded:
        base -= 0.05
    if toc_found and anchor_resolved and end_boundary_found:
        base += 0.03  # both anchors → bonus

    return max(0.0, min(1.0, round(base, 3)))


# ══════════════════════════════════════════════════════════════════════════════
#  Entry points  (Section H)
# ══════════════════════════════════════════════════════════════════════════════


def _get_spec(section_name: str) -> SectionSpec:
    spec = SECTION_SPECS.get(section_name)
    if spec is None:
        raise ValueError(
            f"Unsupported section_name '{section_name}'. Supported: {', '.join(SECTION_NAMES)}"
        )
    return spec


def _build_section_key(metadata: DocumentMetadata | None, section_name: str) -> str | None:
    if metadata is None:
        return None
    fy = metadata.filing_date.year - 1 if metadata.filing_date.month <= 8 else metadata.filing_date.year
    return f"{metadata.cik}-{fy}-{section_name}"


def _node_anchor(node: Tag) -> str | None:
    for attr in ("id", "name"):
        val = node.get(attr)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def extract_section(
    raw_html: str,
    metadata: DocumentMetadata | None = None,
    section_name: str = "compensation_discussion_and_analysis",
) -> SectionExtractionResult:
    """Extract a target compensation section as markdown.

    Drop-in replacement for ``cda_markdown_extractor.extract_section_markdown()``.
    """
    spec = _get_spec(section_name)
    section_key = _build_section_key(metadata, section_name)

    warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
    soup = BeautifulSoup(raw_html, "lxml")

    strategy_steps: list[str] = []
    extraction_warnings: list[str] = []

    # ── step 1: parse TOC ───────────────────────────────────────────────
    toc_entries, toc_table_ids = _parse_toc_tables(soup)
    toc_found = len(toc_entries) > 0

    # ── step 2: match section in TOC ────────────────────────────────────
    start_entry = _match_section_in_toc(toc_entries, spec)
    start_node: Tag | None = None
    start_anchor: str | None = None
    start_page: int | None = None
    anchor_resolved = False

    if start_entry is not None:
        start_page = start_entry.page
        start_anchor = start_entry.href.lstrip("#") if start_entry.href else None
        strategy_steps.append("toc_matched")

        # ── step 3: resolve anchor ─────────────────────────────────────
        anchor_node = _resolve_anchor_target(soup, start_entry.href)
        if anchor_node is not None:
            anchor_resolved = True
            # walk forward from anchor to find actual content start
            start_node = _find_content_start_from_anchor(anchor_node, spec)
            if start_node is not None:
                strategy_steps.append("anchor_resolved")
            else:
                start_node = anchor_node  # use anchor itself as fallback
                strategy_steps.append("anchor_direct")

    # ── step 4: heading fallback ────────────────────────────────────────
    if start_node is None:
        start_node = _find_heading_in_body(soup, spec, toc_table_ids)
        if start_node is not None:
            strategy_steps.append("heading_fallback")
            if start_anchor is None:
                start_anchor = _node_anchor(start_node)

    if start_node is None:
        return SectionExtractionResult(
            section_name=section_name,
            section_key=section_key,
            section_found=False,
            markdown="",
            start_anchor=start_anchor,
            end_anchor=None,
            start_page=start_page,
            end_page=None,
            strategy="start_not_found",
            warnings=["Start boundary could not be resolved."],
            confidence=0.0,
        )

    # ── step 5: resolve end boundary ────────────────────────────────────
    end_entry = _find_end_toc_entry(toc_entries, start_entry, spec)
    end_anchor: str | None = end_entry.href.lstrip("#") if (end_entry and end_entry.href) else None
    end_page: int | None = end_entry.page if end_entry else None
    end_node: Tag | None = None
    end_boundary_found = False

    if end_entry is not None:
        end_anchor_node = _resolve_anchor_target(soup, end_entry.href)
        if end_anchor_node is not None:
            # walk forward to find the actual heading that marks the end
            end_content = _find_content_start_from_anchor_generic(end_anchor_node)
            end_node = end_content if end_content is not None else end_anchor_node
            end_boundary_found = True
            strategy_steps.append("toc_end_resolved")

    # ── step 6: DOM linear walk ─────────────────────────────────────────
    collected = _collect_section_nodes(start_node, end_node, spec, toc_table_ids)

    if not collected:
        return SectionExtractionResult(
            section_name=section_name,
            section_key=section_key,
            section_found=False,
            markdown="",
            start_anchor=start_anchor,
            end_anchor=end_anchor,
            start_page=start_page,
            end_page=end_page,
            strategy=" -> ".join(strategy_steps) + " -> no_content_collected",
            warnings=["DOM walk collected zero content nodes."],
            confidence=0.0,
        )

    strategy_steps.append("dom_walk")

    # ── step 7: render to markdown ──────────────────────────────────────
    markdown, render_warnings = _render_to_markdown(collected)
    extraction_warnings.extend(render_warnings)
    docling_ok = not any("fallback" in w.lower() for w in render_warnings)
    if docling_ok:
        strategy_steps.append("docling_md")
    else:
        strategy_steps.append("fallback_md")

    # ── step 8: word-count guard ────────────────────────────────────────
    word_count = len(markdown.split())
    if word_count < _MIN_WORDS_GUARD:
        extraction_warnings.append(
            f"Section '{section_name}' is short ({word_count} words)."
        )

    # ── step 9: confidence ──────────────────────────────────────────────
    confidence = _compute_confidence(
        toc_found=toc_found,
        anchor_resolved=anchor_resolved,
        content_start_found=start_node is not None,
        end_boundary_found=end_boundary_found,
        strategy_steps=strategy_steps,
        word_count=word_count,
        docling_succeeded=docling_ok,
    )

    strategy = " -> ".join(strategy_steps) if strategy_steps else "unknown"
    return SectionExtractionResult(
        section_name=section_name,
        section_key=section_key,
        section_found=bool(markdown),
        markdown=markdown,
        start_anchor=start_anchor,
        end_anchor=end_anchor,
        start_page=start_page,
        end_page=end_page,
        strategy=strategy,
        warnings=extraction_warnings,
        confidence=confidence,
    )


def _find_content_start_from_anchor_generic(anchor_node: Tag) -> Tag | None:
    """Walk forward from an anchor node to find the first content-bearing element.

    Unlike ``_find_content_start_from_anchor``, this does NOT score against a
    spec – it simply finds the first non-noise element.  Used for end-boundary
    anchor resolution.
    """
    current: Tag | None = anchor_node
    hops = 0
    while hops < _MAX_ANCHOR_HOPS:
        current = _next_element_sibling(current)
        if current is None:
            break
        hops += 1
        if _is_page_break_noise(current):
            continue
        text = current.get_text(" ", strip=True)
        if text and len(text.strip()) >= 3:
            return current
    return None


def extract_all_sections(
    raw_html: str,
    metadata: DocumentMetadata | None = None,
) -> dict[str, SectionExtractionResult]:
    """Extract all 5 compensation sections from a single filing.

    Parses HTML only once and reuses the soup for each section.
    """
    results: dict[str, SectionExtractionResult] = {}
    for name in SECTION_NAMES:
        results[name] = extract_section(raw_html, metadata, name)
    return results


# ── backward-compatibility wrappers ─────────────────────────────────────────

def extract_section_markdown(
    raw_html: str,
    metadata: DocumentMetadata | None = None,
    section_name: str = "compensation_discussion_and_analysis",
) -> SectionExtractionResult:
    """Backward-compatible alias for ``extract_section``."""
    return extract_section(raw_html, metadata, section_name)


def extract_cda_markdown(
    raw_html: str,
    metadata: DocumentMetadata | None = None,
) -> SectionExtractionResult:
    """Backward-compatible CD&A-only wrapper."""
    return extract_section(raw_html, metadata, "compensation_discussion_and_analysis")
