"""Base HTML parser — ported from stark-translate-agent file_parser.py (HTMLParser).

Preserves the BeautifulSoup tag traversal, table extraction, and ContentBlock
output contract. SEC-specific enrichment lives in sec_proxy_parser.py.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup, Tag

from core.models import BlockType, ContentBlock

_SKIP_TAGS = {"script", "style", "nav", "footer", "head", "meta", "link"}
_HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


def _tag_ancestors(tag: Tag) -> list[str]:
    return [p.name for p in tag.parents if p.name and p.name != "[document]"]


def _extract_table(tag: Tag) -> tuple[list[list[str]], str]:
    """Extract table as (rows: list[list[str]], linearized_text: str)."""
    rows: list[list[str]] = []
    for tr in tag.find_all("tr"):
        cells = [cell.get_text(" ", strip=True) for cell in tr.find_all(["td", "th"])]
        if cells:
            rows.append(cells)
    linearized = " | ".join(" | ".join(row) for row in rows)
    return rows, linearized


class HTMLParser:
    """Parse HTML into a list of ContentBlocks."""

    def parse(self, file_path: Path) -> list[ContentBlock]:
        html = file_path.read_text(encoding="utf-8", errors="replace")
        soup = BeautifulSoup(html, "lxml")
        blocks: list[ContentBlock] = []

        for tag in soup.find_all(True):
            if not isinstance(tag, Tag):
                continue
            if tag.name in _SKIP_TAGS:
                continue
            if any(ancestor in _SKIP_TAGS for ancestor in _tag_ancestors(tag)):
                continue

            meta: dict[str, Any] = {
                "tag": tag.name,
                "classes": tag.get("class", []),
                "parent_tags": _tag_ancestors(tag)[:3],
            }

            if tag.name in _HEADING_TAGS:
                text = tag.get_text(" ", strip=True)
                if text:
                    blocks.append(ContentBlock(
                        type=BlockType.HEADING,
                        text=text,
                        metadata={**meta, "level": int(tag.name[1])},
                    ))

            elif tag.name == "table":
                # Avoid processing nested tables twice
                if any(p.name == "table" for p in tag.parents):
                    continue
                rows, linearized = _extract_table(tag)
                if rows:
                    blocks.append(ContentBlock(
                        type=BlockType.TABLE,
                        text=linearized,
                        metadata={**meta, "rows": rows,
                                  "cols": max((len(r) for r in rows), default=0)},
                    ))

            elif tag.name in {"p", "li"}:
                # Skip if this tag is inside a table
                if any(p.name == "table" for p in tag.parents):
                    continue
                text = tag.get_text(" ", strip=True)
                if len(text) > 20:  # filter noise
                    block_type = (
                        BlockType.LIST_ITEM if tag.name == "li" else BlockType.PARAGRAPH
                    )
                    blocks.append(ContentBlock(type=block_type, text=text, metadata=meta))

        return blocks
