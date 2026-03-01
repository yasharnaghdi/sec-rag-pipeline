from __future__ import annotations

from datetime import date
from pathlib import Path

from ingestion.metadata_model import DocumentMetadata, FootnoteBlock, HeadingBlock, ImageBlock, TableBlock, XBRLTaggedBlock
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


def _sample_html() -> str:
    fixture_path = Path(__file__).resolve().parents[1] / "fixtures" / "sample_cnob.html"
    return fixture_path.read_text(encoding="utf-8")


def _parse_sample() -> list[object]:
    parser = SECHTMLParser()
    return parser.parse(_sample_html(), _metadata())


def test_h2_produces_heading_block_with_level_2() -> None:
    blocks = _parse_sample()
    heading = next(block for block in blocks if isinstance(block, HeadingBlock) and block.text == "EXECUTIVE COMPENSATION")
    assert heading.level == 2
    assert heading.detection_method == "tag"


def test_bold_allcaps_p_produces_heading_block_bold_heuristic() -> None:
    blocks = _parse_sample()
    heading = next(block for block in blocks if isinstance(block, HeadingBlock) and block.text == "SUMMARY COMPENSATION TABLE")
    assert heading.detection_method == "bold_heuristic"


def test_table_produces_table_block_with_correct_row_count() -> None:
    blocks = _parse_sample()
    table = next(block for block in blocks if isinstance(block, TableBlock))
    assert len(table.rows) == 2


def test_table_colspan_expands_cells() -> None:
    blocks = _parse_sample()
    table = next(block for block in blocks if isinstance(block, TableBlock))
    assert len(table.rows[0]) == 4
    assert table.rows[0][2] == "Total Compensation"
    assert table.rows[0][3] == "Total Compensation"


def test_table_header_row_count_is_1() -> None:
    blocks = _parse_sample()
    table = next(block for block in blocks if isinstance(block, TableBlock))
    assert table.header_row_count == 1


def test_footnote_p_after_table_linked_to_table_id() -> None:
    blocks = _parse_sample()
    table = next(block for block in blocks if isinstance(block, TableBlock))
    footnote = next(block for block in blocks if isinstance(block, FootnoteBlock) and block.marker == "(1)")
    assert footnote.linked_table_id == table.id


def test_table_footnotes_dict_populated() -> None:
    blocks = _parse_sample()
    table = next(block for block in blocks if isinstance(block, TableBlock))
    assert "(1)" in table.footnotes
    assert table.footnotes["(1)"] == "(1) Amounts reflect base salary only."


def test_ix_nonfraction_produces_xbrl_tagged_block() -> None:
    blocks = _parse_sample()
    xbrl_block = next(block for block in blocks if isinstance(block, XBRLTaggedBlock))
    assert len(xbrl_block.xbrl_tags) == 1
    assert xbrl_block.xbrl_tags[0].concept_name == "us-gaap:SalaryAndWages"
    assert xbrl_block.xbrl_tags[0].context_ref == "FY2024"


def test_img_produces_image_block_with_position_token() -> None:
    blocks = _parse_sample()
    image = next(block for block in blocks if isinstance(block, ImageBlock))
    assert image.position_token.startswith("[IMAGE:")


def test_order_index_monotonically_increasing() -> None:
    blocks = _parse_sample()
    order_indexes = [block.order_index for block in blocks]
    assert all(curr > prev for prev, curr in zip(order_indexes, order_indexes[1:]))


def test_section_id_preamble_before_first_heading() -> None:
    html = "<html><body><p>Introductory preface text.</p>" + _sample_html().replace("<html><body>", "", 1)
    parser = SECHTMLParser()
    blocks = parser.parse(html, _metadata())
    first_heading_index = next(i for i, block in enumerate(blocks) if isinstance(block, HeadingBlock))
    assert first_heading_index > 0
    assert all(block.section_id == "preamble" for block in blocks[:first_heading_index])


def test_section_id_matches_heading_id_after_heading() -> None:
    blocks = _parse_sample()
    first_heading_index = next(i for i, block in enumerate(blocks) if isinstance(block, HeadingBlock))
    first_heading = blocks[first_heading_index]
    assert isinstance(first_heading, HeadingBlock)
    assert blocks[first_heading_index + 1].section_id == first_heading.id
