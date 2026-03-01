"""SEC DEF 14A HTML parser that emits deterministic ingestion block models."""
from __future__ import annotations

import re
from typing import Iterable, Literal

from bs4 import BeautifulSoup
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
_FOOTNOTE_PATTERN = re.compile(r"^\s*([\(\*†‡]\d*[\)\.]?)\s+")
_DetectionMethod = Literal[
    "tag",
    "bold_heuristic",
    "allcaps_heuristic",
    "keyword_match",
]


class SECHTMLParser:
    """Parse raw SEC proxy HTML into typed ingestion blocks."""

    def parse(self, raw_html: str, metadata: DocumentMetadata) -> list[BaseBlock]:
        soup = BeautifulSoup(raw_html, "lxml")
        blocks: list[BaseBlock] = []
        order_index = 0
        current_section_id = "preamble"
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
                heading = HeadingBlock(
                    document_id=metadata.document_id,
                    section_id=current_section_id,
                    order_index=order_index,
                    source_char_start=source_start,
                    source_char_end=source_end,
                    text=text,
                    level=int(tag.name[1]),
                    detection_method="tag",
                )
                blocks.append(heading)
                current_section_id = heading.id
                order_index += 1
                continue

            if tag.name == "table":
                rows, header_row_count, linearized_text, has_merged_cells = _extract_table_rows(tag)
                if not rows:
                    continue
                table_block = TableBlock(
                    document_id=metadata.document_id,
                    section_id=current_section_id,
                    order_index=order_index,
                    source_char_start=source_start,
                    source_char_end=source_end,
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
                heading = HeadingBlock(
                    document_id=metadata.document_id,
                    section_id=current_section_id,
                    order_index=order_index,
                    source_char_start=source_start,
                    source_char_end=source_end,
                    text=text,
                    level=level,
                    detection_method=detection_method,
                )
                blocks.append(heading)
                current_section_id = heading.id
                order_index += 1
                continue

            prose = ProseBlock(
                document_id=metadata.document_id,
                section_id=current_section_id,
                order_index=order_index,
                source_char_start=source_start,
                source_char_end=source_end,
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

    bold_method = _has_sole_bold_child(tag)
    if bold_method is not None and _is_all_caps_heading(text):
        return 2, bold_method

    if tag.name == "p" and any(pattern.search(text) for pattern in _SEC_SECTION_REGEXES):
        return 2, "keyword_match"

    return None


def _extract_table_rows(table_tag: Tag) -> tuple[list[list[str]], int, str, bool]:
    rows: list[list[str]] = []
    header_row_count = 0
    has_merged_cells = False

    for tr in table_tag.find_all("tr"):
        row: list[str] = []
        row_cells = tr.find_all(["th", "td"])
        if not row_cells:
            continue

        all_header = True
        for cell in row_cells:
            text = cell.get_text(" ", strip=True)
            colspan_raw = cell.get("colspan", "1")
            try:
                colspan = int(str(colspan_raw))
            except ValueError:
                colspan = 1
            colspan = max(1, colspan)
            if colspan > 1:
                has_merged_cells = True
            row.extend([text] * colspan)
            if cell.name != "th":
                all_header = False

        rows.append(row)
        if all_header:
            header_row_count += 1

    linearized_text = " | ".join(cell for row in rows for cell in row)
    return rows, header_row_count, linearized_text, has_merged_cells


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
