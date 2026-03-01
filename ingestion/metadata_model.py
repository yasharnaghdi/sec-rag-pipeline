"""Deterministic metadata block models for SEC filing ingestion."""
from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, model_validator


def _stable_block_id(block_type: str, payload: dict[str, object]) -> str:
    canonical_payload = {"block_type": block_type, **payload}
    canonical_json = json.dumps(canonical_payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


class BaseBlock(BaseModel):
    id: str = ""
    document_id: str
    section_id: str
    order_index: int
    source_char_start: int
    source_char_end: int

    @model_validator(mode="after")
    def assign_deterministic_id(self) -> BaseBlock:
        payload = self.model_dump(mode="json")
        payload.pop("id", None)
        self.id = _stable_block_id(self.__class__.__name__, payload)
        return self


class ProseBlock(BaseBlock):
    text: str
    token_count: int


class HeadingBlock(BaseBlock):
    text: str
    level: int = Field(ge=1, le=6)
    detection_method: Literal[
        "tag",
        "bold_heuristic",
        "allcaps_heuristic",
        "keyword_match",
    ]


class TableBlock(BaseBlock):
    rows: list[list[str]]
    header_row_count: int
    linearized_text: str
    footnotes: dict[str, str] = Field(default_factory=dict)
    has_merged_cells: bool
    token_count_linearized: int


class ImageBlock(BaseBlock):
    alt_text: str
    position_token: str
    caption_text: str | None


class FootnoteBlock(BaseBlock):
    marker: str
    text: str
    linked_table_id: str | None


class XBRLAnnotation(BaseModel):
    concept_name: str
    value: str
    context_ref: str


class XBRLTaggedBlock(BaseBlock):
    text: str
    xbrl_tags: list[XBRLAnnotation]
    token_count: int


class FilingMetadata(BaseModel):
    """Metadata passed from downloader to parser for each SEC filing."""

    slot: int | None = None
    cik: str
    company_name: str
    ticker: str | None = None
    industry: str | None = None
    form_type: str
    filing_date: date
    accession_number: str
    edgar_url: str
    raw_html_path: str | None = None


class DocumentMetadata(BaseModel):
    document_id: str
    cik: str
    company_name: str
    form_type: str
    filing_date: date
    accession_number: str
    source_url: str
    fiscal_year_end: date | None = None
    raw_html_path: str | None = None
