"""SEC-aware chunking for typed ingestion blocks."""
from __future__ import annotations

from typing import Any
from uuid import uuid4

import tiktoken
from langchain_text_splitters import RecursiveCharacterTextSplitter
from pydantic import BaseModel, Field

from ingestion.metadata_model import (
    BaseBlock,
    DocumentMetadata,
    FootnoteBlock,
    HeadingBlock,
    ImageBlock,
    ProseBlock,
    TableBlock,
    XBRLTaggedBlock,
)

_ENCODING: Any | None = None
_ENCODING_INITIALIZED = False


def _get_encoding() -> Any | None:
    """Return cl100k_base encoding when available; fallback to None offline."""
    global _ENCODING
    global _ENCODING_INITIALIZED
    if _ENCODING_INITIALIZED:
        return _ENCODING

    try:
        _ENCODING = tiktoken.get_encoding("cl100k_base")
    except Exception:
        _ENCODING = None
    _ENCODING_INITIALIZED = True
    return _ENCODING


def tiktoken_len(text: str) -> int:
    """Return token count using cl100k_base."""
    encoding = _get_encoding()
    if encoding is None:
        return len(text.split())
    return len(encoding.encode(text))


class Chunk(BaseModel):
    """Embeddable chunk emitted from a parsed SEC block."""

    id: str = Field(default_factory=lambda: str(uuid4()))
    source_block_id: str
    document_id: str
    section_id: str
    text: str
    token_count: int
    chunk_index: int
    citation_string: str
    table_json: str | None = None


class SECChunker:
    """Split parsed SEC blocks into token-bounded chunks."""

    def __init__(
        self,
        chunk_size: int = 600,
        chunk_overlap: int = 100,
    ) -> None:
        if chunk_overlap >= chunk_size:
            msg = "chunk_overlap must be smaller than chunk_size"
            raise ValueError(msg)
        self._chunk_size = chunk_size
        self._chunk_overlap = chunk_overlap
        self._splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            length_function=tiktoken_len,
        )

    def chunk_blocks(self, blocks: list[BaseBlock], metadata: DocumentMetadata) -> list[Chunk]:
        """Chunk parsed blocks; tables are always atomic."""
        chunks: list[Chunk] = []
        chunk_index = 0

        for block in blocks:
            if isinstance(block, TableBlock):
                text = block.linearized_text
                token_count = min(tiktoken_len(text), self._chunk_size)
                chunks.append(
                    self._build_chunk(
                        block=block,
                        metadata=metadata,
                        text=text,
                        token_count=token_count,
                        chunk_index=chunk_index,
                        table_json=block.model_dump_json(),
                    )
                )
                chunk_index += 1
                continue

            text = _block_text(block)
            if not text:
                continue

            for split_text in self._split_text(text):
                chunks.append(
                    self._build_chunk(
                        block=block,
                        metadata=metadata,
                        text=split_text,
                        token_count=tiktoken_len(split_text),
                        chunk_index=chunk_index,
                        table_json=None,
                    )
                )
                chunk_index += 1

        return chunks

    def _build_chunk(
        self,
        block: BaseBlock,
        metadata: DocumentMetadata,
        text: str,
        token_count: int,
        chunk_index: int,
        table_json: str | None,
    ) -> Chunk:
        """Construct a single chunk with citation metadata."""
        citation_string = (
            f"{metadata.company_name} | {metadata.form_type} | {metadata.filing_date} | "
            f"{block.section_id} | chunk {chunk_index}"
        )
        return Chunk(
            source_block_id=block.id,
            document_id=metadata.document_id,
            section_id=block.section_id,
            text=text,
            token_count=token_count,
            chunk_index=chunk_index,
            citation_string=citation_string,
            table_json=table_json,
        )

    def _split_text(self, text: str) -> list[str]:
        """Split text and enforce the hard token upper bound."""
        split_texts = self._splitter.split_text(text)
        bounded_splits: list[str] = []
        for split in split_texts:
            if tiktoken_len(split) <= self._chunk_size:
                bounded_splits.append(split)
                continue
            bounded_splits.extend(
                _split_by_tokens(
                    split,
                    chunk_size=self._chunk_size,
                    chunk_overlap=self._chunk_overlap,
                )
            )
        return bounded_splits


def _block_text(block: BaseBlock) -> str:
    if isinstance(block, (ProseBlock, HeadingBlock, FootnoteBlock, XBRLTaggedBlock)):
        return block.text
    if isinstance(block, ImageBlock):
        parts: list[str] = [block.position_token]
        if block.caption_text:
            parts.append(block.caption_text)
        elif block.alt_text:
            parts.append(block.alt_text)
        return " ".join(parts).strip()
    return ""


def _split_by_tokens(text: str, chunk_size: int, chunk_overlap: int) -> list[str]:
    encoding = _get_encoding()
    if encoding is None:
        return _split_by_words(text, chunk_size, chunk_overlap)

    tokens = encoding.encode(text)
    if len(tokens) <= chunk_size:
        return [text]

    pieces: list[str] = []
    start = 0
    while start < len(tokens):
        end = min(start + chunk_size, len(tokens))
        piece = encoding.decode(tokens[start:end]).strip()
        if piece:
            pieces.append(piece)
        if end >= len(tokens):
            break
        start = max(0, end - chunk_overlap)
    return pieces


def _split_by_words(text: str, chunk_size: int, chunk_overlap: int) -> list[str]:
    words = text.split()
    if len(words) <= chunk_size:
        return [text]

    pieces: list[str] = []
    start = 0
    while start < len(words):
        end = min(start + chunk_size, len(words))
        piece = " ".join(words[start:end]).strip()
        if piece:
            pieces.append(piece)
        if end >= len(words):
            break
        start = max(0, end - chunk_overlap)
    return pieces
