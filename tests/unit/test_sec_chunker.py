from __future__ import annotations

import re
from datetime import date
from pathlib import Path

from ingestion.metadata_model import DocumentMetadata, ProseBlock, TableBlock
from ingestion.sec_chunker import SECChunker
from ingestion.sec_html_parser import SECHTMLParser


def _metadata() -> DocumentMetadata:
    return DocumentMetadata(
        document_id="712771_000143774925011656",
        cik="712771",
        company_name="ConnectOne Bancorp, Inc.",
        form_type="DEF 14A",
        filing_date=date(2025, 4, 11),
        accession_number="0001437749-25-011656",
        source_url="https://www.sec.gov/Archives/edgar/data/712771/000143774925011656/cnob20240411_def14a.htm",
        fiscal_year_end=None,
        raw_html_path="tests/fixtures/sample_cnob.html",
    )


def _prose_block(text: str, section_id: str = "preamble", order_index: int = 0) -> ProseBlock:
    return ProseBlock(
        document_id="712771_000143774925011656",
        section_id=section_id,
        order_index=order_index,
        source_char_start=0,
        source_char_end=len(text),
        toc_page_range=None,
        text=text,
        token_count=max(1, len(text.split())),
    )


def _table_block(section_id: str = "sec_table", order_index: int = 1) -> TableBlock:
    rows = [["Name", "Title", "Total Compensation"], ["John Smith", "CEO", "500,000"]]
    linearized = " | ".join(cell for row in rows for cell in row)
    return TableBlock(
        document_id="712771_000143774925011656",
        section_id=section_id,
        order_index=order_index,
        source_char_start=0,
        source_char_end=100,
        toc_page_range=None,
        rows=rows,
        header_row_count=1,
        linearized_text=linearized,
        footnotes={"(1)": "(1) Amounts reflect base salary only."},
        has_merged_cells=False,
        token_count_linearized=max(1, len(linearized.split())),
    )


def test_prose_block_produces_one_chunk() -> None:
    blocks = [_prose_block("This is a short block for chunking.")]
    chunks = SECChunker().chunk_blocks(blocks, _metadata())
    assert len(chunks) == 1


def test_table_block_produces_exactly_one_chunk_atomic() -> None:
    blocks = [_table_block()]
    chunks = SECChunker().chunk_blocks(blocks, _metadata())
    assert len(chunks) == 1


def test_table_chunk_has_table_json_populated() -> None:
    blocks = [_table_block()]
    chunk = SECChunker().chunk_blocks(blocks, _metadata())[0]
    assert chunk.table_json is not None


def test_chunk_indices_monotonically_increasing_from_zero() -> None:
    long_text = "word " * 1400
    blocks = [
        _prose_block(long_text.strip(), section_id="sec_long", order_index=0),
        _table_block(section_id="sec_table", order_index=1),
        _prose_block("Final short block.", section_id="sec_tail", order_index=2),
    ]
    chunks = SECChunker().chunk_blocks(blocks, _metadata())
    assert [chunk.chunk_index for chunk in chunks] == list(range(len(chunks)))


def test_prose_chunk_max_1200_tokens() -> None:
    long_text = "token " * 1800
    chunks = SECChunker().chunk_blocks([_prose_block(long_text.strip())], _metadata())
    assert all(chunk.token_count <= 1200 for chunk in chunks)


def test_chunk_inherits_section_id_from_block() -> None:
    blocks = [_prose_block("A short sectioned paragraph.", section_id="section_exec_comp")]
    chunk = SECChunker().chunk_blocks(blocks, _metadata())[0]
    assert chunk.section_id == "section_exec_comp"


def test_chunk_citation_string_is_non_null() -> None:
    blocks = [_prose_block("A short citation paragraph.", section_id="section_governance")]
    chunk = SECChunker().chunk_blocks(blocks, _metadata())[0]
    assert chunk.citation_string


def test_chunk_citation_string_format() -> None:
    metadata = _metadata()
    blocks = [_prose_block("A short citation paragraph.", section_id="section_governance")]
    chunk = SECChunker().chunk_blocks(blocks, metadata)[0]
    expected = (
        f"{metadata.company_name} | {metadata.form_type} | {metadata.filing_date} | "
        f"{chunk.section_id} | chunk {chunk.chunk_index}"
    )
    assert chunk.citation_string == expected


def test_table_chunk_uses_table_chunk_size_limit() -> None:
    long_linearized = "token " * 3200
    table_block = TableBlock(
        document_id="712771_000143774925011656",
        section_id="sec_table",
        order_index=0,
        source_char_start=0,
        source_char_end=100,
        toc_page_range=None,
        rows=[["token", "token"], ["token", "token"]],
        header_row_count=1,
        linearized_text=long_linearized.strip(),
        footnotes={},
        has_merged_cells=False,
        token_count_linearized=3200,
    )
    chunk = SECChunker().chunk_blocks([table_block], _metadata())[0]
    assert chunk.token_count == 2400


def test_chunk_citation_includes_page_range_when_available() -> None:
    metadata = _metadata()
    block = _prose_block("Short paragraph.", section_id="section_exec_comp")
    block.toc_page_range = (42, 43)
    chunk = SECChunker().chunk_blocks([block], metadata)[0]
    expected = (
        f"{metadata.company_name} | {metadata.form_type} | {metadata.filing_date} | "
        f"{chunk.section_id} | pp.42-43 | chunk {chunk.chunk_index}"
    )
    assert chunk.citation_string == expected
    assert chunk.toc_page_range == (42, 43)


def test_chunk_toc_page_range_none_when_not_in_toc() -> None:
    metadata = _metadata()
    block = _prose_block("Short paragraph.", section_id="section_exec_comp")
    chunk = SECChunker().chunk_blocks([block], metadata)[0]
    assert chunk.toc_page_range is None
    expected = (
        f"{metadata.company_name} | {metadata.form_type} | {metadata.filing_date} | "
        f"{chunk.section_id} | chunk {chunk.chunk_index}"
    )
    assert chunk.citation_string == expected


def test_real_filing_chunks_preserve_compensation_and_cda_text() -> None:
    filing_path = Path("data/raw/0000320193_000130817926000008.html")
    assert filing_path.exists()

    raw_html = filing_path.read_text(encoding="utf-8", errors="replace")
    metadata = DocumentMetadata(
        document_id="320193_000130817926000008",
        cik="320193",
        company_name="Apple Inc.",
        form_type="DEF 14A",
        filing_date=date(2026, 1, 10),
        accession_number="0001308179-26-000008",
        source_url="https://www.sec.gov/Archives/edgar/data/320193/000130817926000008/aapl-20260108.htm",
        fiscal_year_end=None,
        raw_html_path=str(filing_path),
    )

    blocks = SECHTMLParser().parse(raw_html, metadata)
    chunks = SECChunker().chunk_blocks(blocks, metadata)

    assert any(
        "Tim Cook" in chunk.text
        and "Chief Executive Officer" in chunk.text
        and "74294811" in re.sub(r"\D", "", chunk.text)
        for chunk in chunks
    )
    assert any(
        "pay for performance" in chunk.text.lower() or "pay-for-performance" in chunk.text.lower()
        for chunk in chunks
    )
