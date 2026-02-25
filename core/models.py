"""Pydantic models for typed data flow across the pipeline."""
from __future__ import annotations

from datetime import date
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class BlockType(str, Enum):
    HEADING = "heading"
    PARAGRAPH = "paragraph"
    TABLE = "table"
    FOOTNOTE = "footnote"
    LIST_ITEM = "list_item"


class FilingMetadata(BaseModel):
    """Sourced from edgartools at download time; injected into every block."""
    cik: str
    company_name: str
    filing_date: date
    document_type: str  # DEF14A, 10-K, etc.
    accession_number: str


class ContentBlock(BaseModel):
    """V1 base block — matches stark-translate-agent ContentBlock semantics but typed."""
    id: UUID = Field(default_factory=uuid4)
    type: BlockType
    text: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class SECBlock(ContentBlock):
    """Extended block with SEC filing attribution for RAG."""
    cik: str
    company_name: str
    filing_date: date
    document_type: str
    section_header: str | None = None
    section_order: int = 0
    footnotes: list[str] = Field(default_factory=list)
    # Table-specific dual-format fields
    rows: list[list[str]] | None = None          # JSON rows for structured queries
    linearized_text: str | None = None           # Flattened text for embedding


class Chunk(BaseModel):
    """Post-split unit, ready for embedding."""
    id: UUID = Field(default_factory=uuid4)
    source_block_id: UUID
    section_id: UUID | None = None
    text: str
    chunk_type: BlockType
    token_count: int
    chunk_index: int
    table_json: list[list[str]] | None = None
    linearized_text: str | None = None
    # Populated after embedding
    embedding_id: UUID | None = None
