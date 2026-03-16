"""SEC DEF 14A HTML parser that emits deterministic ingestion block models."""
from __future__ import annotations

import re
import warnings
from typing import Iterable, Literal

from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning
from bs4.element import NavigableString, Tag

from ingestion.metadata_model import (
    BaseBlock,
    DocumentMetadata,
    FootnoteBlock,
    HeadingBlock,
    ImageBlock,
    ProseBlock,
    TableBlock,
    XBRLAnnotation,
    XBRLTaggedBlock,
)

SEC_SECTION_PATTERNS = [
    r"COMPENSATION DISCUSSION AND ANALYSIS",
    r"EXECUTIVE COMPENSATION",
    r"SUMMARY COMPENSATION TABLE",
    r"GRANTS OF PLAN.BASED AWARDS",
    r"OUTSTANDING EQUITY AWARDS",
    r"DIRECTOR COMPENSATION",
    r"CORPORATE GOVERNANCE",
    r"BOARD OF DIRECTORS",
    r"AUDIT COMMITTEE",
    r"SECURITY OWNERSHIP",
]

_SEC_SECTION_REGEXES: list[re.Pattern[str]] = [
    re.compile(pattern, re.IGNORECASE) for pattern in SEC_SECTION_PATTERNS
]
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
_PARSEABLE_TAGS = [*sorted(_HEADING_TAGS), "p", "div", "table", "img"]
_XBRL_TAG_NAMES = {"ix:nonnumeric", "ix:nonfraction"}
_XBRL_INLINE_TAGS: frozenset[str] = frozenset({"ix:nonfraction", "ix:nonnumeric"})
_FOOTNOTE_PATTERN = re.compile(r"^\s*([\(\*†‡]\d*[\)\.]?)\s+")
_PAGE_NUMBER_PATTERN = re.compile(
    r"^\s*[-–—]?\s*\d{1,3}\s*[-–—]?\s*$"
    r"|^\s*[Pp]age\s+\d{1,3}\s*$"
    r"|^\s*[Pp]age\s+\d{1,3}\s+of\s+\d{1,3}\s*$"
)
_MIN_HEADING_COLSPAN = 3
_FISCAL_YEAR_HEADER_RE = re.compile(r"\b(20\d{2}|fiscal.?year|year)\b", re.IGNORECASE)
_FISCAL_YEAR_VALUE_RE = re.compile(r"20(?:[0-2]\d|30)")
_DetectionMethod = Literal[
    "tag",
    "bold_heuristic",
    "allcaps_heuristic",
    "keyword_match",
]


def _cell_text(cell: Tag) -> str:
    """Return the best available text for a table cell."""
    for child in cell.find_all(True):
        child_name = (child.name or "").lower()
        if child_name in _XBRL_INLINE_TAGS:
            xbrl_value = child.get_text(" ", strip=True)
            if xbrl_value:
                return xbrl_value

    raw = cell.get_text(" ", strip=True)
    normalised = re.sub(r"[\s\xa0]+", " ", raw).strip()
    if re.search(r"[$€£,]", normalised):
        normalised = re.sub(r"\s*,\s*", ",", normalised)
        normalised = re.sub(r"(?<=\d)\s+(?=\d)", "", normalised)
        normalised = re.sub(r"([$€£])\s+(?=\d)", r"\1", normalised)
    return normalised


def _extract_table_heading_row(table_tag: Tag) -> str | None:
    """Return heading text when table's first rows encode a section heading."""
    rows = table_tag.find_all("tr", recursive=False)
    if not rows:
        rows = table_tag.find_all("tr")

    for tr in rows[:2]:
        cells = tr.find_all(["td", "th"])
        if len(cells) != 1:
            continue
        cell = cells[0]
        colspan = _parse_table_span(cell.get("colspan", "1"))
        if colspan < _MIN_HEADING_COLSPAN:
            continue
        text = cell.get_text(" ", strip=True)
        if not text:
            continue
        if any(pattern.search(text) for pattern in _SEC_SECTION_REGEXES):
            return text
    return None


class SECHTMLParser:
    """Parse raw SEC proxy HTML into typed ingestion blocks."""

    def parse(self, raw_html: str, metadata: DocumentMetadata) -> list[BaseBlock]:
        warnings.filterwarnings("ignore", category=XMLParsedAsHTMLWarning)
        soup = BeautifulSoup(raw_html, "lxml")
        toc_map = _extract_toc(soup)
        blocks: list[BaseBlock] = []
        order_index = 0
        current_section_id = "preamble"
        current_toc_page_range: tuple[int, int] | None = None
        search_cursor = 0
        footnote_links: dict[int, tuple[str, str, str]] = {}

        for tag in soup.find_all(_PARSEABLE_TAGS):
            if not isinstance(tag, Tag):
                continue
            if tag.name != "table" and tag.find_parent("table") is not None:
                continue

            source_start, source_end, search_cursor = _find_tag_span(raw_html, tag, search_cursor)

            if tag.name in _HEADING_TAGS:
                text = _tag_text(tag)
                if not text:
                    continue
                current_toc_page_range = toc_map.get(_normalize_section_label(text))
                heading = HeadingBlock(
                    document_id=metadata.document_id,
                    section_id=current_section_id,
                    order_index=order_index,
                    source_char_start=source_start,
                    source_char_end=source_end,
                    toc_page_range=current_toc_page_range,
                    text=text,
                    level=int(tag.name[1]),
                    detection_method="tag",
                )
                blocks.append(heading)
                current_section_id = heading.id
                order_index += 1
                continue

            if tag.name == "table":
                embedded_heading_text = _extract_table_heading_row(tag)
                if embedded_heading_text is not None:
                    current_toc_page_range = toc_map.get(
                        _normalize_section_label(embedded_heading_text)
                    )
                    embedded_heading = HeadingBlock(
                        document_id=metadata.document_id,
                        section_id=current_section_id,
                        order_index=order_index,
                        source_char_start=source_start,
                        source_char_end=source_end,
                        toc_page_range=current_toc_page_range,
                        text=embedded_heading_text,
                        level=2,
                        detection_method="keyword_match",
                    )
                    blocks.append(embedded_heading)
                    current_section_id = embedded_heading.id
                    order_index += 1

                rows, header_row_count, linearized_text, has_merged_cells = _extract_table_rows(tag)
                if not rows:
                    continue
                table_block = TableBlock(
                    document_id=metadata.document_id,
                    section_id=current_section_id,
                    order_index=order_index,
                    source_char_start=source_start,
                    source_char_end=source_end,
                    toc_page_range=current_toc_page_range,
                    rows=rows,
                    header_row_count=header_row_count,
                    linearized_text=linearized_text,
                    footnotes={},
                    has_merged_cells=has_merged_cells,
                    token_count_linearized=_token_count(linearized_text),
                )
                _link_table_footnotes(tag, table_block, footnote_links)
                blocks.append(table_block)
                order_index += 1
                continue

            if tag.name == "img":
                alt_text = str(tag.get("alt", ""))
                caption_text = _extract_following_caption(tag)
                image_block = ImageBlock(
                    document_id=metadata.document_id,
                    section_id=current_section_id,
                    order_index=order_index,
                    source_char_start=source_start,
                    source_char_end=source_end,
                    toc_page_range=current_toc_page_range,
                    alt_text=alt_text,
                    position_token=f"[IMAGE:{alt_text}]",
                    caption_text=caption_text,
                )
                blocks.append(image_block)
                order_index += 1
                continue

            footnote_link = footnote_links.get(id(tag))
            if footnote_link is not None:
                marker, footnote_text, linked_table_id = footnote_link
                footnote = FootnoteBlock(
                    document_id=metadata.document_id,
                    section_id=current_section_id,
                    order_index=order_index,
                    source_char_start=source_start,
                    source_char_end=source_end,
                    toc_page_range=current_toc_page_range,
                    marker=marker,
                    text=footnote_text,
                    linked_table_id=linked_table_id,
                )
                blocks.append(footnote)
                order_index += 1
                continue

            xbrl_annotations = _extract_xbrl_annotations(tag)
            if xbrl_annotations:
                text = _tag_text(tag)
                if not text:
                    continue
                xbrl_block = XBRLTaggedBlock(
                    document_id=metadata.document_id,
                    section_id=current_section_id,
                    order_index=order_index,
                    source_char_start=source_start,
                    source_char_end=source_end,
                    toc_page_range=current_toc_page_range,
                    text=text,
                    xbrl_tags=xbrl_annotations,
                    token_count=_token_count(text),
                )
                blocks.append(xbrl_block)
                order_index += 1
                continue

            text = _tag_text(tag)
            if not text:
                continue

            heading_info = _classify_heading_from_text(tag, text)
            if heading_info is not None:
                level, detection_method = heading_info
                current_toc_page_range = toc_map.get(_normalize_section_label(text))
                heading = HeadingBlock(
                    document_id=metadata.document_id,
                    section_id=current_section_id,
                    order_index=order_index,
                    source_char_start=source_start,
                    source_char_end=source_end,
                    toc_page_range=current_toc_page_range,
                    text=text,
                    level=level,
                    detection_method=detection_method,
                )
                blocks.append(heading)
                current_section_id = heading.id
                order_index += 1
                continue

            if _PAGE_NUMBER_PATTERN.match(text):
                continue

            prose = ProseBlock(
                document_id=metadata.document_id,
                section_id=current_section_id,
                order_index=order_index,
                source_char_start=source_start,
                source_char_end=source_end,
                toc_page_range=current_toc_page_range,
                text=text,
                token_count=_token_count(text),
            )
            blocks.append(prose)
            order_index += 1

        return blocks


def _tag_text(tag: Tag) -> str:
    return tag.get_text(" ", strip=True)


def _token_count(text: str) -> int:
    return len(text.split())


def _has_sole_bold_child(tag: Tag) -> _DetectionMethod | None:
    children: list[Tag | NavigableString] = []
    for child in tag.children:
        if isinstance(child, Tag):
            children.append(child)
        elif isinstance(child, NavigableString) and child.strip():
            children.append(child)

    if len(children) != 1:
        return None
    only_child = children[0]
    if not isinstance(only_child, Tag):
        return None
    if only_child.name in {"b", "strong"}:
        return "bold_heuristic"
    return None


def _is_all_caps_heading(text: str) -> bool:
    if len(text.strip()) <= 10:
        return False
    letters_only = re.sub(r"[^A-Za-z]+", "", text)
    return bool(letters_only) and letters_only.isupper()


def _classify_heading_from_text(
    tag: Tag,
    text: str,
) -> tuple[int, _DetectionMethod] | None:
    if tag.name not in {"p", "div"}:
        return None
    if _PAGE_NUMBER_PATTERN.match(text):
        return None

    bold_method = _has_sole_bold_child(tag)
    if bold_method is not None and _is_all_caps_heading(text):
        return 2, bold_method

    if tag.name == "p" and any(pattern.search(text) for pattern in _SEC_SECTION_REGEXES):
        return 2, "keyword_match"

    return None


def _normalize_section_label(text: str) -> str:
    normalized = re.sub(r"\s+", " ", text.strip().upper())
    normalized = re.sub(r"[\s\.\u2022·•\-–—]+$", "", normalized)
    return normalized


def _extract_toc(soup: BeautifulSoup) -> dict[str, tuple[int, int]]:
    """Extract a normalized section-to-page-range map from the first ToC-like table."""
    toc: dict[str, tuple[int, int]] = {}
    page_ref = re.compile(r"\b(\d{1,3})\s*$")

    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if len(rows) < 4:
            continue

        candidate_entries: list[tuple[str, int]] = []
        for tr in rows:
            cells = tr.find_all(["td", "th"])
            if not cells:
                continue
            row_text = " ".join(cell.get_text(" ", strip=True) for cell in cells)
            match = page_ref.search(row_text)
            if match is None:
                continue

            label = _normalize_section_label(row_text[: match.start()])
            if not label:
                continue
            candidate_entries.append((label, int(match.group(1))))

        if len(candidate_entries) < 4:
            continue

        sorted_entries = sorted(candidate_entries, key=lambda item: item[1])
        for index, (label, start_page) in enumerate(sorted_entries):
            if index + 1 < len(sorted_entries):
                next_start_page = sorted_entries[index + 1][1]
                end_page = max(start_page, next_start_page - 1)
            else:
                end_page = start_page + 1
            toc[label] = (start_page, end_page)
        break

    return toc


def _extract_table_rows(table_tag: Tag) -> tuple[list[list[str]], int, str, bool]:
    rows: list[list[str]] = []
    header_row_count = 0
    has_merged_cells = False
    rowspan_carry: dict[int, tuple[int, str]] = {}

    for tr in table_tag.find_all("tr"):
        tr_cells = tr.find_all(["td", "th"])
        if len(tr_cells) == 1:
            single_cell = tr_cells[0]
            colspan = _parse_table_span(single_cell.get("colspan", "1"))
            if colspan >= _MIN_HEADING_COLSPAN:
                heading_candidate = single_cell.get_text(" ", strip=True)
                if any(pattern.search(heading_candidate) for pattern in _SEC_SECTION_REGEXES):
                    continue

        row: list[str] = []
        row_cells = tr_cells
        if not row_cells and not rowspan_carry:
            continue

        col_index = 0
        all_header = bool(row_cells)

        def inject_rowspan_carry() -> None:
            nonlocal col_index
            while col_index in rowspan_carry:
                remaining_rows, span_text = rowspan_carry[col_index]
                row.append(span_text)
                if remaining_rows <= 1:
                    del rowspan_carry[col_index]
                else:
                    rowspan_carry[col_index] = (remaining_rows - 1, span_text)
                col_index += 1

        inject_rowspan_carry()
        for cell in row_cells:
            inject_rowspan_carry()
            text = _cell_text(cell)
            colspan = _parse_table_span(cell.get("colspan", "1"))
            rowspan = _parse_table_span(cell.get("rowspan", "1"))

            if colspan > 1 or rowspan > 1:
                has_merged_cells = True
            for offset in range(colspan):
                row.append(text)
                if rowspan > 1:
                    rowspan_carry[col_index + offset] = (rowspan - 1, text)
            col_index += colspan
            if cell.name != "th":
                all_header = False

        inject_rowspan_carry()
        if not row:
            continue
        rows.append(row)
        if all_header:
            header_row_count += 1

    rows, header_row_count = _expand_multi_year_rows(rows, header_row_count)
    linearized_text = " | ".join(cell for row in rows for cell in row)
    return rows, header_row_count, linearized_text, has_merged_cells


def _split_cell_for_year_count(cell_text: str, year_index: int, year_count: int) -> str:
    text = cell_text.strip()
    if year_count <= 1 or not text:
        return text

    parts = [part.strip() for part in re.split(r"[\r\n|;]+", text) if part.strip()]
    if len(parts) == year_count:
        return parts[year_index]

    numeric_tokens = re.findall(r"\(?\$?\d[\d,]*(?:\.\d+)?\)?", text)
    if len(numeric_tokens) == year_count:
        return str(numeric_tokens[year_index]).strip()
    return text


def _extract_year_candidates(text: str) -> list[str]:
    stripped = text.strip()
    if not stripped:
        return []
    if re.fullmatch(r"20(?:[0-2]\d|30)", stripped):
        return [stripped]

    years = _FISCAL_YEAR_VALUE_RE.findall(stripped)
    deduped: list[str] = []
    for year in years:
        if year not in deduped:
            deduped.append(year)
    return deduped


def _expand_multi_year_rows(rows: list[list[str]], header_row_count: int) -> tuple[list[list[str]], int]:
    if not rows:
        return rows, header_row_count

    header_rows = max(1, header_row_count or 1)
    header_rows = min(header_rows, len(rows))
    header_slice = rows[:header_rows]
    max_cols = max((len(row) for row in rows), default=0)

    year_columns: set[int] = set()
    for col_index in range(max_cols):
        column_header_parts: list[str] = []
        for header_row in header_slice:
            if col_index < len(header_row):
                value = header_row[col_index].strip()
                if value:
                    column_header_parts.append(value)
        if _FISCAL_YEAR_HEADER_RE.search(" ".join(column_header_parts)):
            year_columns.add(col_index)

    if not year_columns:
        return rows, header_row_count

    expanded_rows: list[list[str]] = list(header_slice)
    for row in rows[header_rows:]:
        if not any(cell.strip() for cell in row):
            continue

        detected_years: list[str] = []
        for year_col in sorted(year_columns):
            if year_col >= len(row):
                continue
            for year in _extract_year_candidates(row[year_col]):
                if year not in detected_years:
                    detected_years.append(year)

        if not detected_years:
            expanded_rows.append(row)
            continue

        for year_index, year in enumerate(detected_years):
            expanded = list(row)
            for col_index, cell in enumerate(expanded):
                if col_index in year_columns:
                    expanded[col_index] = year
                else:
                    expanded[col_index] = _split_cell_for_year_count(cell, year_index, len(detected_years))
            expanded_rows.append(expanded)

    return expanded_rows, header_row_count


def _parse_table_span(raw_value: object) -> int:
    try:
        span = int(str(raw_value))
    except (TypeError, ValueError):
        span = 1
    return max(1, span)


def _iter_next_sibling_tags(tag: Tag, limit: int) -> Iterable[Tag]:
    count = 0
    sibling: Tag | NavigableString | None = tag
    while count < limit:
        if sibling is None:
            return
        sibling = sibling.find_next_sibling()
        if sibling is None:
            return
        if isinstance(sibling, Tag):
            yield sibling
            count += 1


def _link_table_footnotes(
    table_tag: Tag,
    table_block: TableBlock,
    footnote_links: dict[int, tuple[str, str, str]],
) -> None:
    for sibling in _iter_next_sibling_tags(table_tag, limit=3):
        if sibling.name != "p":
            continue
        text = sibling.get_text(" ", strip=True)
        match = _FOOTNOTE_PATTERN.match(text)
        if match is None:
            continue
        marker = match.group(1).strip()
        table_block.footnotes[marker] = text
        footnote_links[id(sibling)] = (marker, text, table_block.id)


def _extract_xbrl_annotations(tag: Tag) -> list[XBRLAnnotation]:
    annotations: list[XBRLAnnotation] = []
    for child in tag.find_all(True):
        child_name = (child.name or "").lower()
        if child_name not in _XBRL_TAG_NAMES:
            continue
        concept_name = str(child.get("name", ""))
        context_ref = str(child.get("contextRef") or child.get("contextref") or "")
        value = child.get_text(" ", strip=True)
        annotations.append(
            XBRLAnnotation(
                concept_name=concept_name,
                value=value,
                context_ref=context_ref,
            )
        )
    return annotations


def _extract_following_caption(image_tag: Tag) -> str | None:
    sibling = image_tag.find_next_sibling()
    if not isinstance(sibling, Tag):
        return None
    if sibling.name not in {"figcaption", "p"}:
        return None
    caption = sibling.get_text(" ", strip=True)
    return caption or None


def _find_tag_span(raw_html: str, tag: Tag, search_start: int) -> tuple[int, int, int]:
    serialized_tag = str(tag)
    start = raw_html.find(serialized_tag, search_start)
    if start >= 0:
        end = start + len(serialized_tag)
        return start, end, end

    opening_marker = f"<{tag.name}"
    start = raw_html.find(opening_marker, search_start)
    if start < 0:
        safe_pos = max(0, min(search_start, len(raw_html)))
        return safe_pos, safe_pos, safe_pos

    close_index = raw_html.find(f"</{tag.name}>", start)
    if close_index >= 0:
        end = close_index + len(tag.name) + 3
    else:
        gt_index = raw_html.find(">", start)
        end = gt_index + 1 if gt_index >= 0 else len(raw_html)

    end = max(start, min(end, len(raw_html)))
    return start, end, end
