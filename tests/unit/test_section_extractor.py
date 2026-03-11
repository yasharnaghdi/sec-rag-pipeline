"""Tests for ingestion.section_extractor (DOM-linear-walk strategy)."""
from __future__ import annotations

from datetime import date

from ingestion.section_extractor import (
    SECTION_NAMES,
    extract_all_sections,
    extract_cda_markdown,
    extract_section,
    extract_section_markdown,
)
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


# ── Family A: standard TOC with anchors on <p> elements ────────────────────

_FAMILY_A_HTML = """
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
  <p>Executive compensation narrative text goes here with enough words to pass the short-text guard.
     This paragraph provides additional context about the company executive compensation philosophy
     and strategy for the current fiscal year and beyond in detail.</p>
  <p id="cda">Compensation Discussion and Analysis</p>
  <p>CD&amp;A narrative text with compensation rationale and analysis of key decisions.
     This section provides a comprehensive overview of the compensation committee's deliberations
     throughout the year regarding executive pay packages and performance metrics.</p>
  <p id="dir">Director Compensation</p>
  <p>Director compensation narrative text describing how non-employee directors are compensated
     including retainer fees, committee service fees, equity grants, and other benefits provided
     to members of the board of directors.</p>
  <p id="pay">Pay Versus Performance</p>
  <p>Pay versus performance narrative text including required tabular disclosures and analysis
     of the relationship between executive compensation actually paid and company performance
     over the covered fiscal years as required by SEC rules.</p>
  <p id="eq">Equity Compensation Plans</p>
  <p>Equity compensation plans narrative text covering the company stock incentive plan, employee
     stock purchase plan, and other equity-based compensation arrangements available to executives
     and eligible employees of the company and its subsidiaries.</p>
  <p id="item4">Item 4: Ratification of Auditor</p>
  <p>Non-compensation section starts here.</p>
</body></html>
"""


def test_family_a_all_sections_found() -> None:
    meta = _metadata()
    for name in SECTION_NAMES:
        result = extract_section(_FAMILY_A_HTML, meta, section_name=name)
        assert result.section_found, f"{name} not found"
        assert result.markdown, f"{name} has empty markdown"
        assert result.section_name == name


def test_family_a_start_anchors() -> None:
    meta = _metadata()
    expected = {
        "compensation_discussion_and_analysis": "cda",
        "executive_compensation": "exec",
        "director_compensation": "dir",
        "pay_vs_performance": "pay",
        "equity_compensation_plans": "eq",
    }
    for name, anchor in expected.items():
        result = extract_section(_FAMILY_A_HTML, meta, section_name=name)
        assert result.start_anchor == anchor, f"{name}: expected {anchor}, got {result.start_anchor}"


def test_family_a_section_key() -> None:
    meta = _metadata()
    result = extract_section(_FAMILY_A_HTML, meta, "executive_compensation")
    assert result.section_key == f"{meta.cik}-2024-executive_compensation"


# ── Family B: empty <div> anchors, all content in <div> ────────────────────

_FAMILY_B_HTML = """
<html><body>
  <table>
    <tr><td>Section</td><td>Page</td></tr>
    <tr><td><a href="#sec1">Executive Compensation</a></td><td>5</td></tr>
    <tr><td><a href="#sec2">Compensation Discussion and Analysis</a></td><td>10</td></tr>
    <tr><td><a href="#sec3">Director Compensation</a></td><td>20</td></tr>
    <tr><td><a href="#sec4">Pay Versus Performance</a></td><td>25</td></tr>
    <tr><td><a href="#sec5">Equity Compensation Plans</a></td><td>30</td></tr>
    <tr><td><a href="#sec6">Item 4: Ratification of Auditor</a></td><td>40</td></tr>
  </table>

  <div id="sec1"></div>
  <hr style="page-break-after: always"/>
  <div></div>
  <div style="font-size:16pt">Executive Compensation</div>
  <div>Executive compensation content from a Workiva-formatted filing with extensive
       narrative about the company compensation philosophy and program design approach.</div>

  <div id="sec2"></div>
  <hr style="page-break-after: always"/>
  <div></div>
  <div style="font-size:16pt">Compensation Discussion and Analysis</div>
  <div>CD&amp;A content describing compensation committee analysis and rationale for
       the executive pay decisions made during the fiscal year under review.</div>

  <div id="sec3"></div>
  <div style="font-size:16pt">Director Compensation</div>
  <div>Director compensation content describing fees and equity grants for non-employee
       directors serving on the board of directors and its committees.</div>

  <div id="sec4"></div>
  <div style="font-size:16pt">Pay Versus Performance</div>
  <div>Pay versus performance content with required tabular disclosure showing the
       relationship between pay and company performance metrics.</div>

  <div id="sec5"></div>
  <div style="font-size:16pt">Equity Compensation Plans</div>
  <div>Equity compensation plans content describing the stock incentive plan and employee
       stock purchase plan for all eligible employees and executives.</div>

  <div id="sec6"></div>
  <div style="font-size:16pt">Item 4: Ratification of Auditor</div>
  <div>Non-compensation content about auditor ratification vote.</div>
</body></html>
"""


def test_family_b_empty_div_anchors() -> None:
    """Family B filings have empty <div id> anchors several siblings from content."""
    result = extract_section(_FAMILY_B_HTML, None, "compensation_discussion_and_analysis")
    assert result.section_found, f"CD&A not found. Strategy: {result.strategy}"
    assert result.markdown
    assert "anchor_resolved" in result.strategy or "anchor_direct" in result.strategy


def test_family_b_all_sections_found() -> None:
    for name in SECTION_NAMES:
        result = extract_section(_FAMILY_B_HTML, None, section_name=name)
        assert result.section_found, f"{name} not found. Strategy: {result.strategy}"


# ── Family C: no-link TOC, bold headings ────────────────────────────────────

_FAMILY_C_HTML = """
<html><body>
  <table>
    <tr><td>Section</td><td>Page</td></tr>
    <tr><td>Executive Compensation</td><td>5</td></tr>
    <tr><td>Compensation Discussion and Analysis</td><td>10</td></tr>
    <tr><td>Director Compensation</td><td>20</td></tr>
    <tr><td>Pay Versus Performance</td><td>25</td></tr>
    <tr><td>Equity Compensation Plans</td><td>30</td></tr>
    <tr><td>Item 4: Ratification of Auditor</td><td>40</td></tr>
  </table>

  <p style="text-align:center"><b>EXECUTIVE COMPENSATION</b></p>
  <p>Executive compensation content with bold centered heading format
     commonly found in smaller filer proxy statements filed with the SEC.</p>

  <p style="text-align:center"><b>COMPENSATION DISCUSSION AND ANALYSIS</b></p>
  <p>CD&amp;A content for Family C with bold centered heading style describing
     the compensation committee analysis for executive officer pay decisions.</p>

  <p style="text-align:center"><b>DIRECTOR COMPENSATION</b></p>
  <p>Director compensation content for non-employee members of the board
     of directors including retainer fees and equity grants.</p>

  <p style="text-align:center"><b>PAY VERSUS PERFORMANCE</b></p>
  <p>Pay versus performance content with required SEC disclosure tables
     showing the relationship between compensation and performance.</p>

  <p style="text-align:center"><b>EQUITY COMPENSATION PLANS</b></p>
  <p>Equity compensation plans content covering stock option plans and
     restricted stock unit plans for eligible employees and executives.</p>

  <p style="text-align:center"><b>RATIFICATION OF AUDITOR</b></p>
  <p>Non-compensation section about auditor.</p>
</body></html>
"""


def test_family_c_no_link_toc_bold_headings() -> None:
    """Family C has TOC without href links; headings are bold+centered <p>."""
    result = extract_section(_FAMILY_C_HTML, None, "compensation_discussion_and_analysis")
    assert result.section_found, f"CD&A not found. Strategy: {result.strategy}"
    assert result.markdown


def test_family_c_uses_heading_fallback() -> None:
    result = extract_section(_FAMILY_C_HTML, None, "executive_compensation")
    assert result.section_found
    assert "heading_fallback" in result.strategy


# ── backward-compat wrappers ───────────────────────────────────────────────

def test_extract_section_markdown_wrapper() -> None:
    result = extract_section_markdown(_FAMILY_A_HTML, None, "executive_compensation")
    assert result.section_found


def test_extract_cda_markdown_wrapper() -> None:
    result = extract_cda_markdown(_FAMILY_A_HTML, None)
    assert result.section_name == "compensation_discussion_and_analysis"
    assert result.section_found


def test_extract_all_sections() -> None:
    results = extract_all_sections(_FAMILY_A_HTML, _metadata())
    assert set(results.keys()) == set(SECTION_NAMES)
    for name, result in results.items():
        assert result.section_found, f"{name} not found"


# ── edge cases ──────────────────────────────────────────────────────────────

def test_empty_html_returns_not_found() -> None:
    result = extract_section("<html><body></body></html>", None)
    assert not result.section_found
    assert result.confidence == 0.0


def test_unsupported_section_name_raises() -> None:
    import pytest
    with pytest.raises(ValueError, match="Unsupported section_name"):
        extract_section("<html><body></body></html>", None, "nonexistent_section")


def test_noise_elements_skipped() -> None:
    """Page-break noise elements should not appear in extracted markdown."""
    html = """
    <html><body>
      <table>
        <tr><td>Section</td><td>Page</td></tr>
        <tr><td><a href="#cda">Compensation Discussion and Analysis</a></td><td>10</td></tr>
        <tr><td><a href="#next">Director Compensation</a></td><td>20</td></tr>
        <tr><td><a href="#item4">Item 4: Other Business</a></td><td>25</td></tr>
        <tr><td><a href="#item5">Item 5: Proposals</a></td><td>30</td></tr>
      </table>
      <p id="cda">Compensation Discussion and Analysis</p>
      <p>Real content before page break with analysis of executive pay decisions.</p>
      <hr/>
      <h5><a href="#toc">Table of Contents</a></h5>
      <p>15</p>
      <p>Real content after page break continuing the analysis discussion.</p>
      <p id="next">Director Compensation</p>
      <p>Director compensation starts here.</p>
      <p id="item4">Item 4: Other Business</p>
      <p id="item5">Item 5: Proposals</p>
    </body></html>
    """
    result = extract_section(html, None, "compensation_discussion_and_analysis")
    assert result.section_found
    assert "Table of Contents" not in result.markdown
