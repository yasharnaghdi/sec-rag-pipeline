"""Offline tests for XBRL cell extraction and wide-td heading detection."""
from __future__ import annotations

from datetime import date

import pytest
from bs4 import BeautifulSoup
from bs4.element import Tag

from ingestion.metadata_model import DocumentMetadata, HeadingBlock, TableBlock
from ingestion.sec_html_parser import SECHTMLParser, _cell_text


@pytest.fixture()
def doc_meta() -> DocumentMetadata:
    return DocumentMetadata(
        document_id="test-doc-001",
        cik="0000320193",
        company_name="Test Corp",
        form_type="DEF 14A",
        filing_date=date(2023, 4, 15),
        accession_number="0000320193-23-000001",
        source_url="https://example.com/proxy.htm",
    )


def _td_from_html(html: str) -> Tag:
    cell = BeautifulSoup(html, "lxml").find("td")
    assert isinstance(cell, Tag)
    return cell


class TestCellText:
    """Tests for _cell_text() XBRL preference and fallback normalization."""

    def test_prefers_xbrl_nonfraction_over_gettext(self) -> None:
        html = (
            '<td><ix:nonfraction name="us-gaap:SalariesAndWages" '
            'contextRef="FY2023">1250000</ix:nonfraction></td>'
        )
        assert _cell_text(_td_from_html(html)) == "1250000"

    def test_prefers_xbrl_nonnumeric_over_gettext(self) -> None:
        html = (
            '<td><ix:nonnumeric name="dei:EntityCommonStockSharesOutstanding" '
            'contextRef="FY2023">Jane Smith</ix:nonnumeric></td>'
        )
        assert _cell_text(_td_from_html(html)) == "Jane Smith"

    def test_falls_back_to_gettext_for_plain_cell(self) -> None:
        assert _cell_text(_td_from_html("<td>  Base Salary  </td>")) == "Base Salary"

    def test_normalises_nbsp_in_plain_cell(self) -> None:
        result = _cell_text(_td_from_html("<td>$\xa01\xa0,\xa02\xa05\xa00\xa0,\xa00\xa00\xa00</td>"))
        assert "\xa0" not in result
        assert "1" in result and "250" in result


class TestEmbeddedHeadingDetection:
    """Tests for heading promotion from wide <td> table rows."""

    def test_summary_comp_heading_in_td_creates_heading_block(
        self, doc_meta: DocumentMetadata
    ) -> None:
        html = """
        <html><body>
        <table>
          <tr><td colspan="8"><b>SUMMARY COMPENSATION TABLE</b></td></tr>
          <tr><th>Name</th><th>Year</th><th>Salary</th><th>Total</th></tr>
          <tr><td>Jane Smith</td><td>2023</td>
              <td><ix:nonfraction name="us-gaap:SalariesAndWages">1250000
              </ix:nonfraction></td>
              <td>1800000</td></tr>
        </table>
        </body></html>
        """
        blocks = SECHTMLParser().parse(html, doc_meta)

        heading_blocks = [b for b in blocks if isinstance(b, HeadingBlock)]
        table_blocks = [b for b in blocks if isinstance(b, TableBlock)]
        heading_texts = [h.text for h in heading_blocks]

        assert any("SUMMARY COMPENSATION" in t.upper() for t in heading_texts)
        comp_heading = next(h for h in heading_blocks if "SUMMARY COMPENSATION" in h.text.upper())
        comp_tables = [t for t in table_blocks if t.section_id == comp_heading.id]
        assert comp_tables

    def test_heading_block_precedes_table_block_by_order_index(
        self, doc_meta: DocumentMetadata
    ) -> None:
        html = """
        <html><body>
        <table>
          <tr><td colspan="5">SUMMARY COMPENSATION TABLE</td></tr>
          <tr><th>Name</th><th>Year</th><th>Salary</th></tr>
          <tr><td>Bob Jones</td><td>2023</td><td>900000</td></tr>
        </table>
        </body></html>
        """
        blocks = SECHTMLParser().parse(html, doc_meta)

        heading_blocks = [b for b in blocks if isinstance(b, HeadingBlock)]
        table_blocks = [b for b in blocks if isinstance(b, TableBlock)]

        comp_heading = next(
            (h for h in heading_blocks if "SUMMARY COMPENSATION" in h.text.upper()),
            None,
        )
        assert comp_heading is not None
        comp_table = next((t for t in table_blocks if t.section_id == comp_heading.id), None)
        assert comp_table is not None
        assert comp_heading.order_index < comp_table.order_index

    def test_heading_row_not_included_as_data_row(
        self, doc_meta: DocumentMetadata
    ) -> None:
        html = """
        <html><body>
        <table>
          <tr><td colspan="4">SUMMARY COMPENSATION TABLE</td></tr>
          <tr><th>Name</th><th>Year</th><th>Salary</th><th>Total</th></tr>
          <tr><td>Alice Wang</td><td>2023</td><td>1100000</td><td>1500000</td></tr>
        </table>
        </body></html>
        """
        blocks = SECHTMLParser().parse(html, doc_meta)
        table = next((b for b in blocks if isinstance(b, TableBlock)), None)
        assert table is not None

        flat_cells = [cell for row in table.rows for cell in row]
        assert not any("SUMMARY COMPENSATION" in cell.upper() for cell in flat_cells)

    def test_non_heading_wide_td_does_not_create_extra_heading(
        self, doc_meta: DocumentMetadata
    ) -> None:
        html = """
        <html><body>
        <table>
          <tr><td colspan="4">Amounts in thousands unless stated</td></tr>
          <tr><th>Name</th><th>Year</th><th>Salary</th></tr>
          <tr><td>John Doe</td><td>2023</td><td>500000</td></tr>
        </table>
        </body></html>
        """
        blocks = SECHTMLParser().parse(html, doc_meta)
        heading_blocks = [b for b in blocks if isinstance(b, HeadingBlock)]
        assert not any("amounts in thousands" in h.text.lower() for h in heading_blocks)

    def test_xbrl_salary_value_in_correctly_parented_table(
        self, doc_meta: DocumentMetadata
    ) -> None:
        html = """
        <html><body>
        <table>
          <tr><td colspan="4"><b>SUMMARY COMPENSATION TABLE</b></td></tr>
          <tr><th>Name and Principal Position</th><th>Year</th>
              <th>Salary</th><th>Total</th></tr>
          <tr>
            <td>Jane CEO</td>
            <td>2023</td>
            <td><ix:nonfraction name="us-gaap:SalariesAndWages"
                contextRef="FY2023">1250000</ix:nonfraction></td>
            <td>1800000</td>
          </tr>
        </table>
        </body></html>
        """
        blocks = SECHTMLParser().parse(html, doc_meta)
        heading_blocks = [b for b in blocks if isinstance(b, HeadingBlock)]
        table_blocks = [b for b in blocks if isinstance(b, TableBlock)]

        comp_heading = next(
            (h for h in heading_blocks if "SUMMARY COMPENSATION" in h.text.upper()),
            None,
        )
        assert comp_heading is not None

        comp_table = next((t for t in table_blocks if t.section_id == comp_heading.id), None)
        assert comp_table is not None

        data_rows = comp_table.rows[comp_table.header_row_count :]
        assert data_rows
        salary_cell = data_rows[0][2]
        assert salary_cell == "1250000"
