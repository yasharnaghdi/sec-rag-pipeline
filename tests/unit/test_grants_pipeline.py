from __future__ import annotations

import csv
import sys
from pathlib import Path
from typing import Any

import pytest

from ingestion.comp_table_extractor import extract_grants_plan_based
from ingestion.metadata_model import HeadingBlock, TableBlock
from scripts import run_batch50_key_results as run_batch


def _heading(order_index: int = 0) -> HeadingBlock:
    return HeadingBlock(
        document_id="doc-grants-001",
        section_id="root",
        order_index=order_index,
        source_char_start=0,
        source_char_end=30,
        text="Grants of Plan-Based Awards",
        level=2,
        detection_method="keyword_match",
    )


def _table(rows: list[list[str]], section_id: str, order_index: int, header_row_count: int = 2) -> TableBlock:
    linearized = " | ".join(cell for row in rows for cell in row)
    return TableBlock(
        document_id="doc-grants-001",
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


def test_extract_grants_maps_non_equity_and_equity_triplets_separately() -> None:
    heading = _heading()
    rows = [
        [
            "Name",
            "Grant Date",
            "Estimated future payouts under non-equity incentive plan awards",
            "",
            "",
            "Estimated future payouts under equity incentive plan awards",
            "",
            "",
            "All other stock awards: Number of shares of stock or units",
            "All other option awards: Number of securities underlying options",
            "Exercise or base price of option awards",
            "Grant date fair value of stock and option awards",
            "Grant Type",
        ],
        [
            "",
            "",
            "Threshold",
            "Target",
            "Maximum",
            "Threshold",
            "Target",
            "Maximum",
            "",
            "",
            "",
            "",
            "",
        ],
        [
            "Jane Doe",
            "2024-01-01",
            "100",
            "200",
            "300",
            "400",
            "500",
            "600",
            "700",
            "800",
            "12.5",
            "900",
            "Stock Options",
        ],
    ]
    table = _table(rows, section_id=heading.id, order_index=1)

    extracted = extract_grants_plan_based([heading, table], {"cik": "0000001"})
    assert len(extracted) == 1
    row = extracted[0]
    assert row["non_equity_threshold"] == pytest.approx(100.0)
    assert row["non_equity_target"] == pytest.approx(200.0)
    assert row["non_equity_maximum"] == pytest.approx(300.0)
    assert row["equity_threshold"] == pytest.approx(400.0)
    assert row["equity_target"] == pytest.approx(500.0)
    assert row["equity_maximum"] == pytest.approx(600.0)
    assert row["grant_type"] == "Stock Options"


def test_locate_grants_table_uses_required_scoring_terms() -> None:
    heading = _heading()
    weak_table = _table(
        [
            ["Name", "Salary"],
            ["", ""],
            ["Jane Doe", "1000"],
        ],
        section_id=heading.id,
        order_index=1,
    )
    strong_table = _table(
        [
            [
                "Name",
                "Grant Date",
                "Estimated future payouts under non-equity incentive plan awards",
                "Estimated future payouts under equity incentive plan awards",
                "Grant date fair value of stock and option awards",
            ],
            ["", "", "Threshold", "Target", ""],
            ["Jane Doe AIA", "2024-01-01", "100", "200", "300"],
        ],
        section_id=heading.id,
        order_index=2,
    )

    located, located_heading = run_batch._locate_grants_table([heading, weak_table, strong_table])
    assert located is not None
    assert located_heading is not None
    assert located.id == strong_table.id


def test_locate_grants_table_prefers_true_grants_over_aia_summary() -> None:
    aia_summary = _table(
        [
            ["Name", "Target AIA", "Company Multiplier", "Actual AIA"],
            ["S.J. Squeri", "$", "140%", "$"],
            ["C.Y. Le Caillec", "$", "125%", "$"],
        ],
        section_id="sec-aia",
        order_index=1,
        header_row_count=0,
    )
    true_grants = _table(
        [
            ["", "", "", "", "", "", "", ""],
            [
                "Name",
                "Award Type",
                "Grant Date",
                "Estimated Future Payouts Under Non-Equity Incentive Plan Awards",
                "Estimated Future Payouts Under Equity Incentive Plan Awards",
                "All Other Stock Awards: Number of Shares of Stock or Units",
                "Exercise or Base Price of Option Awards",
                "Grant Date Fair Value of Stock and Option Awards",
            ],
            ["", "", "", "Threshold", "Target", "", "", ""],
            ["Jane Doe", "PRSU", "2024-01-01", "", "1000", "500", "", "2000"],
        ],
        section_id="sec-grants",
        order_index=2,
        header_row_count=0,
    )

    located, _ = run_batch._locate_grants_table([aia_summary, true_grants])
    assert located is not None
    assert located.id == true_grants.id


def test_grant_row_from_det_preserves_name_and_classifies_type() -> None:
    row: dict[str, Any] = {
        "exec_name": "Alex Smith",
        "grant_type": "Performance Restricted Stock Units (PRSU)",
        "grant_date": "2024-01-01",
        "non_equity_threshold": "",
        "non_equity_target": "",
        "non_equity_maximum": "",
        "equity_threshold": "1000",
        "equity_target": "2000",
        "equity_maximum": "3000",
        "all_other_stock_awards_shares": "500",
        "all_other_option_awards_securities": "",
        "exercise_or_base_price": "",
        "grant_date_fair_value": "4000",
    }
    mapped = run_batch._grant_row_from_det(row)
    assert mapped["Name"] == "Alex Smith"
    assert mapped["Grant Type"] == "Performance Restricted Stock Units (PRSU)"


def test_grant_row_from_det_preserves_zero_values() -> None:
    row: dict[str, Any] = {
        "exec_name": "Alex Smith",
        "grant_type": "AIA",
        "grant_date": "2024-01-01",
        "non_equity_threshold": 0.0,
        "non_equity_target": 100.0,
        "equity_threshold": 0.0,
    }
    mapped = run_batch._grant_row_from_det(row)
    assert mapped["Estimated future payouts under non-equity incentive plan awards (Threshold)"] == "0.0"
    assert mapped["Estimated future payouts under equity incentive plan awards (Threshold)"] == "0.0"


def test_extract_grants_prefers_numeric_over_currency_symbol_split_cells() -> None:
    rows = [
        [
            "Name",
            "Award Type",
            "Grant Date",
            "Estimated Future Payouts Under Non-Equity Incentive Plan Awards",
            "Estimated Future Payouts Under Non-Equity Incentive Plan Awards",
            "Estimated Future Payouts Under Non-Equity Incentive Plan Awards",
            "Estimated Future Payouts Under Non-Equity Incentive Plan Awards",
            "Estimated Future Payouts Under Non-Equity Incentive Plan Awards",
            "Estimated Future Payouts Under Non-Equity Incentive Plan Awards",
        ],
        [
            "Name",
            "Award Type",
            "Grant Date",
            "Threshold ($)",
            "Threshold ($)",
            "Target ($)",
            "Target ($)",
            "Maximum ($)",
            "Maximum ($)",
        ],
        ["Jane Doe", "AIA", "2024-01-01", "$0", "", "$", "6,000,000", "$", "11,250,000"],
    ]
    table = _table(rows, section_id="sec-grants", order_index=1, header_row_count=2)
    extracted = extract_grants_plan_based([table], {"cik": "0000001"}, selected_table=table)
    assert len(extracted) == 1
    row = extracted[0]
    assert row["non_equity_threshold"] == pytest.approx(0.0)
    assert row["non_equity_target"] == pytest.approx(6000000.0)
    assert row["non_equity_maximum"] == pytest.approx(11250000.0)


def test_extract_grants_selected_table_without_heading_handles_header_row_count_zero() -> None:
    rows = [
        ["", "", "", "", "", "", "", ""],
        [
            "",
            "Estimated Future Payouts Under Non-Equity Incentive Plan Awards",
            "",
            "",
            "Estimated Future Payouts Under Equity Incentive Plan Awards",
            "",
            "",
            "",
        ],
        [
            "Name",
            "Grant Date",
            "Threshold",
            "Target",
            "Threshold",
            "Target",
            "All other stock awards: Number of shares of stock or units",
            "Grant date fair value of stock and option awards",
        ],
        ["Jane Doe", "", "", "", "", "", "", ""],
        ["Incentive Plan", "2024-01-01", "100", "200", "", "", "", "400"],
        ["Performance-Based RSUs", "2024-02-01", "", "", "500", "600", "700", "800"],
    ]
    table = _table(rows, section_id="prose-sec", order_index=1, header_row_count=0)
    extracted = extract_grants_plan_based([table], {"cik": "0000001"}, selected_table=table)
    assert len(extracted) == 2
    assert extracted[0]["exec_name"] == "Jane Doe"
    assert extracted[0]["grant_type"] == "Incentive Plan"
    assert extracted[1]["exec_name"] == "Jane Doe"
    assert extracted[1]["grant_type"] == "Performance-Based RSUs"
    assert extracted[0]["non_equity_threshold"] == pytest.approx(100.0)
    assert extracted[1]["equity_target"] == pytest.approx(600.0)


def test_extract_grants_handles_deep_split_header_and_stip_psu_rows() -> None:
    rows = [
        ["", "", "", "", "", "", "", "", "", "", "", ""],
        ["GRANT", "", "", "", "", "", "", "", "", "", "", ""],
        ["ALL OTHER", "", "", "", "", "", "DATE", "", "", "", "", ""],
        ["ESTIMATED FUTURE PAYOUTS", "", "", "", "", "", "", "", "", "", "", ""],
        ["UNDER NON-EQUITY", "", "", "", "", "", "", "", "", "", "", ""],
        ["INCENTIVE", "", "", "", "", "", "", "", "", "", "", ""],
        ["PLAN AWARDS", "", "", "", "", "", "", "", "", "", "", ""],
        ["THRESHOLD", "TARGET", "MAXIMUM", "THRESHOLD", "TARGET", "MAXIMUM", "", "", "", "", "", ""],
        [
            "NAMED EXECUTIVE OFFICER",
            "",
            "",
            "",
            "",
            "",
            "GRANT DATE",
            "",
            "",
            "",
            "",
            "",
        ],
        ["Jane Doe", "", "", "", "", "", "", "", "", "", "", ""],
        ["Annual STIP Bonus", "100", "200", "300", "", "", "2024-01-01", "", "", "", "", ""],
        ["Annual PSU Grant", "", "", "", "400", "500", "2024-02-01", "", "", "", "", ""],
    ]
    table = _table(rows, section_id="prose-sec", order_index=1, header_row_count=0)
    extracted = extract_grants_plan_based([table], {"cik": "0000001"}, selected_table=table)

    assert len(extracted) == 2
    assert extracted[0]["exec_name"] == "Jane Doe"
    assert extracted[0]["grant_type"] == "Annual STIP Bonus"
    assert extracted[0]["grant_date"] == "2024-01-01"
    assert extracted[1]["exec_name"] == "Jane Doe"
    assert extracted[1]["grant_type"] == "Annual PSU Grant"
    assert extracted[1]["grant_date"] == "2024-02-01"


def test_main_writes_grants_master_and_per_cik_year_csvs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    input_path = tmp_path / "input.csv"
    with input_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["cik"])
        writer.writeheader()
        writer.writerow({"cik": "1234567"})

    def _mock_process_cik(cik: str, model: str, skip_db: bool) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
        del model, skip_db
        result_row = {column: "" for column in run_batch.KEY_RESULTS_COLUMNS}
        result_row.update(
            {
                "cik": cik,
                "company_name": "Acme Corp",
                "status": "ok",
                "ceo_total": "1000",
                "extraction_method": "deterministic",
            }
        )
        log_row = {column: "" for column in run_batch.BATCH_LOG_COLUMNS}
        log_row.update({"cik": cik, "company_name": "Acme Corp", "status": "ok", "extraction_method": "deterministic"})
        grants_rows = [
            {
                "CIK": cik,
                "Company Name": "Acme Corp",
                "Filing URL": "https://example.com/filing",
                "Name": "Alex Smith",
                "Grant Type": "Incentive Plan",
                "Grant Date": "2024-01-01",
                "Estimated future payouts under non-equity incentive plan awards (Threshold)": "100",
                "Estimated future payouts under non-equity incentive plan awards (Target)": "200",
                "Estimated future payouts under non-equity incentive plan awards (Maximum)": "300",
                "Estimated future payouts under equity incentive plan awards (Threshold)": "",
                "Estimated future payouts under equity incentive plan awards (Target)": "",
                "Estimated future payouts under equity incentive plan awards (Maximum)": "",
                "All other stock awards: Number of shares of stock or units": "",
                "All other option awards: Number of securities underlying options": "",
                "Exercise or base price of option awards": "",
                "Grant date fair value of stock and option awards": "400",
                "__cik": cik,
                "__fiscal_year": "2024",
            },
            {
                "CIK": cik,
                "Company Name": "Acme Corp",
                "Filing URL": "https://example.com/filing",
                "Name": "Alex Smith",
                "Grant Type": "Stock Options",
                "Grant Date": "2024-01-01",
                "Estimated future payouts under non-equity incentive plan awards (Threshold)": "",
                "Estimated future payouts under non-equity incentive plan awards (Target)": "",
                "Estimated future payouts under non-equity incentive plan awards (Maximum)": "",
                "Estimated future payouts under equity incentive plan awards (Threshold)": "",
                "Estimated future payouts under equity incentive plan awards (Target)": "",
                "Estimated future payouts under equity incentive plan awards (Maximum)": "",
                "All other stock awards: Number of shares of stock or units": "",
                "All other option awards: Number of securities underlying options": "1000",
                "Exercise or base price of option awards": "12",
                "Grant date fair value of stock and option awards": "500",
                "__cik": cik,
                "__fiscal_year": "2024",
            },
        ]
        return result_row, log_row, grants_rows

    monkeypatch.setattr(run_batch, "process_cik", _mock_process_cik)
    monkeypatch.setattr(run_batch, "BATCH_OUTPUT_BASE", tmp_path / "output")
    monkeypatch.setattr(run_batch, "MIN_CEO_COVERAGE", 0)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_batch50_key_results.py",
            "--input",
            str(input_path),
            "--batch-label",
            "t-grants",
            "--limit",
            "1",
        ],
    )

    with pytest.raises(SystemExit) as exc:
        run_batch.main()
    assert exc.value.code == 0

    out_dir = tmp_path / "output" / "t-grants"
    master_path = out_dir / "grants_plan_based_master.csv"
    grouped_path = out_dir / "grants_plan_based_by_cik_year" / "1234567_2024.csv"
    assert master_path.exists()
    assert grouped_path.exists()

    with master_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == run_batch.GRANTS_OUTPUT_COLUMNS
        rows = list(reader)
        assert len(rows) == 2
        assert rows[0]["CIK"] == "1234567"
        assert rows[0]["Company Name"] == "Acme Corp"
        assert rows[0]["Filing URL"] == "https://example.com/filing"
        assert rows[0]["Name"] == "Alex Smith"

    with grouped_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == run_batch.GRANTS_OUTPUT_COLUMNS
        rows = list(reader)
        assert len(rows) == 2
        assert rows[1]["CIK"] == "1234567"
        assert rows[1]["Company Name"] == "Acme Corp"
        assert rows[1]["Filing URL"] == "https://example.com/filing"
