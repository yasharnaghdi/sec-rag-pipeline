from __future__ import annotations

from datetime import date
from pathlib import Path

from ingestion.metadata_model import (
    DocumentMetadata,
    FootnoteBlock,
    HeadingBlock,
    ImageBlock,
    TableBlock,
    XBRLTaggedBlock,
)
from ingestion.sec_html_parser import SECHTMLParser


def _sample_metadata() -> DocumentMetadata:
    return DocumentMetadata(
        document_id="doc-connectone-2025",
        cik="0000712771",
        company_name="ConnectOne Bancorp, Inc.",
        form_type="DEF 14A",
        filing_date=date(2025, 4, 1),
        accession_number="0000000000-25-000001",
        source_url="https://www.sec.gov/Archives/edgar/data/712771/example.htm",
        fiscal_year_end=date(2024, 12, 31),
        raw_html_path="tests/fixtures/sample_connectone.html",
    )


def _fixture_html() -> str:
    fixture_path = Path(__file__).resolve().parents[1] / "fixtures" / "sample_connectone.html"
    return fixture_path.read_text(encoding="utf-8")


def test_parse_classifies_required_block_types_and_metadata() -> None:
    parser = SECHTMLParser()
    blocks = parser.parse(_fixture_html(), _sample_metadata())

    h2_block = next(
        block
        for block in blocks
        if isinstance(block, HeadingBlock) and block.text == "Executive Compensation"
    )
    assert h2_block.level == 2
    assert h2_block.detection_method == "tag"

    bold_heading = next(
        block
        for block in blocks
        if isinstance(block, HeadingBlock) and block.text == "CORPORATE GOVERNANCE MATTERS"
    )
    assert bold_heading.detection_method == "bold_heuristic"

    keyword_heading = next(
        block
        for block in blocks
        if isinstance(block, HeadingBlock) and block.text == "SUMMARY COMPENSATION TABLE"
    )
    assert keyword_heading.detection_method == "keyword_match"

    table_block = next(block for block in blocks if isinstance(block, TableBlock))
    assert table_block.rows == [
        ["Name", "Amount"],
        ["Salary", "$1,000"],
        ["Total Compensation", "Total Compensation"],
    ]
    assert table_block.header_row_count == 1
    assert table_block.has_merged_cells is True
    assert table_block.linearized_text
    for value in ("Name", "Amount", "Salary", "$1,000", "Total Compensation"):
        assert value in table_block.linearized_text

    footnote_block = next(block for block in blocks if isinstance(block, FootnoteBlock))
    assert footnote_block.linked_table_id == table_block.id
    assert footnote_block.marker in table_block.footnotes
    assert table_block.footnotes[footnote_block.marker] == footnote_block.text

    xbrl_block = next(block for block in blocks if isinstance(block, XBRLTaggedBlock))
    assert len(xbrl_block.xbrl_tags) == 1

    image_block = next(block for block in blocks if isinstance(block, ImageBlock))
    assert image_block.position_token.startswith("[IMAGE:")

    order_indexes = [block.order_index for block in blocks]
    assert all(curr > prev for prev, curr in zip(order_indexes, order_indexes[1:]))

    first_heading_index = next(i for i, block in enumerate(blocks) if isinstance(block, HeadingBlock))
    first_heading_id = blocks[first_heading_index].id
    for block in blocks[:first_heading_index]:
        assert block.section_id == "preamble"
    assert blocks[first_heading_index + 1].section_id == first_heading_id
