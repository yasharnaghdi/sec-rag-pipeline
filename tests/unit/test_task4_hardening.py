"""Unit tests for Task 4 hardening changes."""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from ingestion.comp_table_extractor import _SUMMARY_COMP_COLS, _map_row
from scripts.run_batch50_key_results import (
    KEY_RESULTS_COLUMNS,
    _collapse_to_roles,
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
                "status": "ok",
                "extraction_method": "deterministic",
                "ceo_name": "Jane",
                "ceo_title": "CEO",
                "ceo_total": "3850000",
                "ceo_salary": "$1,250,000",
                "fiscal_year": "2023",
                "cda_token_count": "100",
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
                "status": "ok",
                "extraction_method": "deterministic",
                "ceo_total": "3850000",
                "ceo_name": "Jane",
                "fiscal_year": "FY2023",
                "cda_token_count": "500",
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
