from __future__ import annotations

from datetime import date

from ingestion.cda_markdown_extractor import SECTION_NAMES, extract_cda_markdown, extract_section_markdown
from ingestion.metadata_model import DocumentMetadata


def _metadata() -> DocumentMetadata:
    return DocumentMetadata(
        document_id="0000001234_000000123425000001",
        cik="0000001234",
        company_name="Example Co",
        form_type="DEF 14A",
        filing_date=date(2025, 3, 14),
        accession_number="0000001234-25-000001",
        source_url="https://example.com/filing",
        fiscal_year_end=None,
        raw_html_path="data/raw/example.html",
    )


def _sample_html() -> str:
    return """
    <html><body>
      <table>
        <tr><td>Section</td><td>Page</td></tr>
        <tr><td><a href="#exec">Executive Compensation</a></td><td>10</td></tr>
        <tr><td><a href="#cda">Compensation Discussion and Analysis</a></td><td>12</td></tr>
        <tr><td><a href="#dir">Director Compensation</a></td><td>20</td></tr>
        <tr><td><a href="#pay">Pay Versus Performance</a></td><td>25</td></tr>
        <tr><td><a href="#eq">Equity Compensation Plans</a></td><td>30</td></tr>
        <tr><td><a href="#item4">Item 4: Ratification of Auditor</a></td><td>40</td></tr>
      </table>
      <p id="exec">Executive Compensation</p>
      <p>Executive compensation narrative text.</p>
      <p id="cda">Compensation Discussion and Analysis</p>
      <p>CD&A narrative text with compensation rationale.</p>
      <p id="dir">Director Compensation</p>
      <p>Director compensation narrative text.</p>
      <p id="pay">Pay Versus Performance</p>
      <p>Pay versus performance narrative text.</p>
      <p id="eq">Equity Compensation Plans</p>
      <p>Equity compensation plans narrative text.</p>
      <p id="item4">Item 4: Ratification of Auditor</p>
      <p>Non-compensation section starts here.</p>
    </body></html>
    """


def test_extract_section_markdown_resolves_all_supported_sections_with_keys() -> None:
    html = _sample_html()
    meta = _metadata()

    expected_start_anchor = {
        "compensation_discussion_and_analysis": "cda",
        "executive_compensation": "exec",
        "director_compensation": "dir",
        "pay_vs_performance": "pay",
        "equity_compensation_plans": "eq",
    }

    for section_name in SECTION_NAMES:
        result = extract_section_markdown(html, meta, section_name=section_name)
        assert result.section_name == section_name
        assert result.section_key == f"{meta.cik}-2024-{section_name}"
        assert result.section_found
        assert result.start_anchor == expected_start_anchor[section_name]
        assert result.markdown


def test_end_boundary_mode_major_non_exec_and_next_boundary() -> None:
    html = _sample_html()
    meta = _metadata()

    exec_result = extract_section_markdown(html, meta, section_name="executive_compensation")
    assert exec_result.end_anchor == "dir"
    assert "toc_major_item_end" in exec_result.strategy

    dir_result = extract_section_markdown(html, meta, section_name="director_compensation")
    assert dir_result.end_anchor == "pay"
    assert "toc_next_entry_end" in dir_result.strategy

    pay_result = extract_section_markdown(html, meta, section_name="pay_vs_performance")
    assert pay_result.end_anchor == "eq"
    assert "toc_next_entry_end" in pay_result.strategy


def test_cda_wrapper_remains_backward_compatible_with_typo_tolerant_toc_match() -> None:
    html = """
    <html><body>
      <table>
        <tr><td>Section</td><td>Page</td></tr>
        <tr><td><a href="#typo">Compensatoon Discussion and Analysis</a></td><td>10</td></tr>
        <tr><td><a href="#next">Item 4: Other Business</a></td><td>15</td></tr>
      </table>
      <p id="typo">Compensatoon Discussion and Analysis</p>
      <p>Compensation discussion text.</p>
      <p id="next">Item 4: Other Business</p>
    </body></html>
    """
    meta = _metadata()
    result = extract_cda_markdown(html, meta)
    assert result.section_name == "compensation_discussion_and_analysis"
    assert result.section_key == f"{meta.cik}-2024-compensation_discussion_and_analysis"
    assert result.start_anchor == "typo"
    assert result.end_anchor == "next"
    assert result.section_found
