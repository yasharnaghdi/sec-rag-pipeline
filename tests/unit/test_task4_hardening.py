"""Unit tests for Task 4 hardening changes."""
from __future__ import annotations

import csv
from datetime import date
from pathlib import Path
from typing import Any

from ingestion.comp_table_extractor import _SUMMARY_COMP_COLS, _map_row, extract_summary_compensation
from ingestion.metadata_model import DocumentMetadata, HeadingBlock, TableBlock
from ingestion.sec_html_parser import SECHTMLParser
from scripts.run_batch50_key_results import (
    KEY_RESULTS_COLUMNS,
    _collapse_to_roles,
    _role_fiscal_year,
    _row_from_det,
)
from scripts.validate_key_results import validate


def _make_det_row(**kwargs: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "exec_name": "",
        "exec_title": "",
        "year": "2023",
        "salary": None,
        "bonus": None,
        "stock_awards": None,
        "option_awards": None,
        "total": None,
        "source_section": "",
        "table_block_id": "",
        "footnote_refs": "",
    }
    base.update(kwargs)
    return base


def _summary_heading(order_index: int = 0) -> HeadingBlock:
    return HeadingBlock(
        document_id="doc-summary-001",
        section_id="root",
        order_index=order_index,
        source_char_start=0,
        source_char_end=32,
        text="Summary Compensation Table",
        level=2,
        detection_method="keyword_match",
    )


def _summary_table(
    rows: list[list[str]],
    section_id: str,
    order_index: int,
    header_row_count: int = 1,
) -> TableBlock:
    linearized = " | ".join(cell for row in rows for cell in row)
    return TableBlock(
        document_id="doc-summary-001",
        section_id=section_id,
        order_index=order_index,
        source_char_start=0,
        source_char_end=max(1, len(linearized)),
        rows=rows,
        header_row_count=header_row_count,
        linearized_text=linearized,
        footnotes={},
        has_merged_cells=False,
        token_count_linearized=max(1, len(linearized.split())),
    )


class TestExecNameTitleSplit:
    def test_summary_comp_schema_has_exec_title(self) -> None:
        assert "exec_title" in _SUMMARY_COMP_COLS

    def test_newline_split_populates_title(self) -> None:
        row = ["Jane Smith\nChief Executive Officer", "2023", "1250000", "1800000"]
        column_map = ["exec_name", "year", "salary", "total"]
        result = _map_row(
            row=row,
            column_map=column_map,
            metadata={},
            footnotes={},
            source_section="Summary Comp",
            table_block_id="tb-001",
        )
        assert result["exec_name"] == "Jane Smith"
        assert result["exec_title"] == "Chief Executive Officer"

    def test_comma_split_with_officer_keyword(self) -> None:
        row = ["Bob Lee, Chief Financial Officer", "2023", "800000", "900000"]
        column_map = ["exec_name", "year", "salary", "total"]
        result = _map_row(
            row=row,
            column_map=column_map,
            metadata={},
            footnotes={},
            source_section="Summary Comp",
            table_block_id="tb-002",
        )
        assert result["exec_name"] == "Bob Lee"
        assert result["exec_title"] == "Chief Financial Officer"

    def test_plain_name_no_split(self) -> None:
        row = ["Alice Wang", "2023", "700000", "800000"]
        column_map = ["exec_name", "year", "salary", "total"]
        result = _map_row(
            row=row,
            column_map=column_map,
            metadata={},
            footnotes={},
            source_section="Summary Comp",
            table_block_id="tb-003",
        )
        assert result["exec_name"] == "Alice Wang"
        assert not result.get("exec_title")

    def test_explicit_exec_title_column_not_overwritten(self) -> None:
        row = ["Jane Smith", "2023", "CEO", "1250000"]
        column_map = ["exec_name", "year", "exec_title", "salary"]
        result = _map_row(
            row=row,
            column_map=column_map,
            metadata={},
            footnotes={},
            source_section="Summary Comp",
            table_block_id="tb-004",
        )
        assert result["exec_name"] == "Jane Smith"
        assert result["exec_title"] == "CEO"

    def test_extract_summary_compensation_carries_forward_multiyear_rows(self) -> None:
        heading = _summary_heading()
        rows = [
            [
                "Name and Principal Position",
                "Year",
                "Salary",
                "Bonus",
                "Stock Awards",
                "Option Awards",
                "Non-Equity Incentive Plan Compensation",
                "All Other Compensation",
                "Total",
            ],
            [
                "Jane Smith, Chief Executive Officer",
                "2023",
                "$1,250,000",
                "0",
                "2,100,000",
                "0",
                "600000",
                "15000",
                "3965000",
            ],
            [
                "",
                "2022",
                "$1,100,000",
                "0",
                "1,900,000",
                "0",
                "550000",
                "14000",
                "3564000",
            ],
            [
                "Bob Lee\nChief Financial Officer",
                "2023",
                "800000",
                "0",
                "900000",
                "0",
                "250000",
                "5000",
                "1955000",
            ],
        ]
        table = _summary_table(rows, section_id=heading.id, order_index=1)

        extracted = extract_summary_compensation([heading, table], {"cik": "0000001"}, selected_table=table)

        assert [row["year"] for row in extracted] == ["2023", "2022", "2023"]
        assert extracted[0]["exec_name"] == "Jane Smith"
        assert extracted[0]["exec_title"] == "Chief Executive Officer"
        assert extracted[1]["exec_name"] == "Jane Smith"
        assert extracted[1]["exec_title"] == "Chief Executive Officer"
        assert extracted[2]["exec_name"] == "Bob Lee"
        assert extracted[2]["exec_title"] == "Chief Financial Officer"

        for row in extracted:
            for field in (
                "salary",
                "bonus",
                "stock_awards",
                "option_awards",
                "non_equity_incentive",
                "other_comp",
                "total",
            ):
                value = row.get(field)
                assert value is None or str(value).isdigit()

    def test_fixture_normalizes_year_tokens_and_prefers_latest_role_year(self) -> None:
        fixture_path = Path(__file__).resolve().parents[1] / "fixtures" / "summary_comp_multi_year.html"
        raw_html = fixture_path.read_text(encoding="utf-8")
        metadata = DocumentMetadata(
            document_id="doc-summary-fixture",
            cik="0000001",
            company_name="Acme Corp",
            form_type="DEF 14A",
            filing_date=date(2025, 4, 11),
            accession_number="0000001-25-000001",
            source_url="https://example.com/def14a",
            fiscal_year_end=None,
            raw_html_path=str(fixture_path),
        )

        blocks = SECHTMLParser().parse(raw_html, metadata)
        extracted = extract_summary_compensation(blocks, {"cik": "0000001"})
        roles = _collapse_to_roles(extracted)

        ceo_rows = [row for row in extracted if row.get("exec_name") == "Jane Smith"]
        cfo_rows = [row for row in extracted if row.get("exec_name") == "Bob Lee"]

        assert [row["year"] for row in ceo_rows] == ["2023", "2022"]
        assert [row["year"] for row in cfo_rows] == ["2023", "2022"]
        assert roles["ceo"]["year"] == "2023"
        assert roles["cfo"]["year"] == "2023"


class TestCollapseToRoles:
    def test_ceo_matched_by_exec_title(self) -> None:
        rows = [
            _make_det_row(
                exec_name="Jane Smith",
                exec_title="Chief Executive Officer",
                total=3850000,
            ),
            _make_det_row(
                exec_name="Bob Lee",
                exec_title="Chief Financial Officer",
                total=900000,
            ),
        ]
        roles = _collapse_to_roles(rows)
        assert roles["ceo"]["exec_name"] == "Jane Smith"
        assert roles["cfo"]["exec_name"] == "Bob Lee"

    def test_president_fallback_to_ceo(self) -> None:
        rows = [
            _make_det_row(
                exec_name="Carol Tan",
                exec_title="President",
                total=2000000,
            ),
        ]
        roles = _collapse_to_roles(rows)
        assert roles["ceo"]["exec_name"] == "Carol Tan"

    def test_others_sorted_by_total_desc(self) -> None:
        rows = [
            _make_det_row(
                exec_name="Jane CEO",
                exec_title="Chief Executive Officer",
                total=3000000,
            ),
            _make_det_row(exec_name="Dave SVP", exec_title="SVP Sales", total=600000),
            _make_det_row(exec_name="Eve EVP", exec_title="EVP Marketing", total=900000),
        ]
        roles = _collapse_to_roles(rows)
        assert roles["other1"]["exec_name"] == "Eve EVP"
        assert roles["other2"]["exec_name"] == "Dave SVP"

    def test_most_recent_year_selected_for_ceo(self) -> None:
        rows = [
            _make_det_row(
                exec_name="Jane CEO",
                exec_title="Chief Executive Officer",
                year="2022",
                total=2000000,
            ),
            _make_det_row(
                exec_name="Jane CEO",
                exec_title="Chief Executive Officer",
                year="2023",
                total=2500000,
            ),
        ]
        roles = _collapse_to_roles(rows)
        assert roles["ceo"]["year"] == "2023"
        assert _role_fiscal_year("deterministic", roles) == "2023"


class TestRowFromDet:
    def test_title_not_duplicated_from_name(self) -> None:
        row = _make_det_row(
            exec_name="Jane Smith",
            exec_title="Chief Executive Officer",
            salary=1250000,
            total=3850000,
        )
        result = _row_from_det(row, "ceo")
        assert result["ceo_name"] == "Jane Smith"
        assert result["ceo_title"] == "Chief Executive Officer"
        assert result["ceo_title"] != result["ceo_name"]

    def test_empty_title_when_no_exec_title(self) -> None:
        row = _make_det_row(exec_name="Jane Smith")
        result = _row_from_det(row, "ceo")
        assert result["ceo_title"] == ""


class TestValidateKeyResults:
    def _write_csv(self, rows: list[dict[str, Any]], tmp_path: Path) -> Path:
        path = tmp_path / "key_results.csv"
        with path.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=KEY_RESULTS_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow(row)
        return path

    def test_dollar_sign_in_numeric_field_fails_check4(self, tmp_path: Path) -> None:
        row = {column: "" for column in KEY_RESULTS_COLUMNS}
        row.update(
            {
                "cik": "1234567",
                "company_name": "Acme Corp",
                "accession_number": "0001234567-24-000001",
                "filing_url": "https://example.com/filing",
                "status": "ok",
                "extraction_method": "deterministic",
                "ceo_name": "Jane",
                "ceo_title": "CEO",
                "ceo_total": "3850000",
                "ceo_salary": "$1,250,000",
                "fiscal_year": "2023",
                "cda_token_count": "100",
                "pay_for_performance_flag": "False",
            }
        )
        path = self._write_csv([row] * 50, tmp_path)
        failures = validate(path, expected_rows=50)
        assert [failure for failure in failures if "CHECK 4" in failure]

    def test_malformed_fiscal_year_fails_check5(self, tmp_path: Path) -> None:
        row = {column: "" for column in KEY_RESULTS_COLUMNS}
        row.update(
            {
                "cik": "1234567",
                "company_name": "Acme Corp",
                "accession_number": "0001234567-24-000001",
                "filing_url": "https://example.com/filing",
                "status": "ok",
                "extraction_method": "deterministic",
                "ceo_total": "3850000",
                "ceo_name": "Jane",
                "fiscal_year": "FY2023",
                "cda_token_count": "500",
                "pay_for_performance_flag": "True",
            }
        )
        path = self._write_csv([row] * 50, tmp_path)
        failures = validate(path, expected_rows=50)
        assert [failure for failure in failures if "CHECK 5" in failure]

    def test_clean_csv_passes_all_checks(self, tmp_path: Path) -> None:
        row = {column: "" for column in KEY_RESULTS_COLUMNS}
        row.update(
            {
                "cik": "1234567",
                "company_name": "Acme Corp",
                "accession_number": "0001234567-24-000001",
                "filing_url": "https://example.com/filing",
                "status": "ok",
                "extraction_method": "deterministic",
                "ceo_name": "Jane Smith",
                "ceo_title": "Chief Executive Officer",
                "ceo_salary": "1250000",
                "ceo_total": "3850000",
                "fiscal_year": "2023",
                "cda_token_count": "800",
                "pay_for_performance_flag": "True",
                "cda_section_found": "True",
            }
        )
        path = self._write_csv([row] * 50, tmp_path)
        failures = validate(path, expected_rows=50)
        assert failures == []

    def test_empty_key_columns_fail_check6(self, tmp_path: Path) -> None:
        row = {column: "" for column in KEY_RESULTS_COLUMNS}
        row.update(
            {
                "cik": "",
                "company_name": "",
                "accession_number": "",
                "filing_url": "",
                "status": "failed",
                "pay_for_performance_flag": "False",
            }
        )
        path = self._write_csv([row] * 50, tmp_path)
        failures = validate(path, expected_rows=50)
        assert [failure for failure in failures if "CHECK 6" in failure]

    def test_ceo_title_copied_from_name_fails_check7(self, tmp_path: Path) -> None:
        row = {column: "" for column in KEY_RESULTS_COLUMNS}
        row.update(
            {
                "cik": "1234567",
                "company_name": "Acme Corp",
                "accession_number": "0001234567-24-000001",
                "filing_url": "https://example.com/filing",
                "status": "ok",
                "extraction_method": "deterministic",
                "ceo_name": "Jane Smith",
                "ceo_title": "Jane Smith",
                "ceo_total": "3850000",
                "fiscal_year": "2023",
                "cda_token_count": "500",
                "pay_for_performance_flag": "True",
            }
        )
        path = self._write_csv([row] * 50, tmp_path)
        failures = validate(path, expected_rows=50)
        assert [failure for failure in failures if "CHECK 7" in failure]

    def test_missing_pay_for_performance_flag_fails_check8(self, tmp_path: Path) -> None:
        row = {column: "" for column in KEY_RESULTS_COLUMNS}
        row.update(
            {
                "cik": "1234567",
                "company_name": "Acme Corp",
                "accession_number": "0001234567-24-000001",
                "filing_url": "https://example.com/filing",
                "status": "ok",
                "extraction_method": "deterministic",
                "ceo_name": "Jane Smith",
                "ceo_title": "Chief Executive Officer",
                "ceo_total": "3850000",
                "fiscal_year": "2023",
                "cda_token_count": "500",
                "pay_for_performance_flag": "",
            }
        )
        path = self._write_csv([row] * 50, tmp_path)
        failures = validate(path, expected_rows=50)
        assert [failure for failure in failures if "CHECK 8" in failure]
