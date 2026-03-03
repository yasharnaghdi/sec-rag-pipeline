#!/usr/bin/env python3
"""Validate compensation extraction CSV outputs."""
from __future__ import annotations

import csv
import re
import warnings
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from bs4 import XMLParsedAsHTMLWarning

from ingestion.metadata_model import DocumentMetadata
from ingestion.sec_html_parser import SECHTMLParser

OUTPUT_DIR = Path("output")
TERMINAL_CHARS = {".", "!", "?", '"', ")"}
MIN_MASTER_COLUMNS = {
    "cik",
    "company_name",
    "ticker",
    "fiscal_year",
    "filing_date",
    "exec_name",
}
NUMERIC_SANITY_COLUMNS = (
    "present_value",
    "grant_fair_value",
    "options_value",
    "stock_vested_value",
)


@dataclass(frozen=True)
class TestResult:
    name: str
    passed: bool
    detail: str = ""


def _set_csv_field_limit() -> None:
    try:
        csv.field_size_limit(50_000_000)
    except OverflowError:
        csv.field_size_limit(2**31 - 1)


def _read_csv_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    if not path.exists():
        return [], []
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = [{k: (v or "") for k, v in row.items()} for row in reader]
        return list(reader.fieldnames or []), rows


def _normalize_exec_name(value: str) -> str:
    cleaned = value.replace("\ufeff", "").strip()
    return cleaned if cleaned else "COMPANY_LEVEL"


def _parse_number(value: str) -> float | None:
    cleaned = value.strip()
    if not cleaned:
        return None
    negative = False
    if cleaned.startswith("(") and cleaned.endswith(")"):
        negative = True
        cleaned = cleaned[1:-1]
    cleaned = cleaned.replace("$", "").replace(",", "").replace("%", "").replace(" ", "")
    cleaned = cleaned.replace("\u00a0", "")
    if not re.search(r"\d", cleaned):
        return None
    try:
        number = float(cleaned)
    except ValueError:
        return None
    return -number if negative else number


def run_test_1_xml_warning_suppressed() -> TestResult:
    parser = SECHTMLParser()
    meta = DocumentMetadata(
        document_id="xml_warning_check",
        cik="0000000000",
        company_name="Warning Check Co",
        form_type="DEF 14A",
        filing_date=date(2025, 1, 1),
        accession_number="0000000000-25-000001",
        source_url="https://example.com",
    )
    raw_html = "<?xml version='1.0' encoding='utf-8'?><root><p>COMPENSATION DISCUSSION AND ANALYSIS</p></root>"

    with warnings.catch_warnings(record=True) as captured:
        warnings.simplefilter("always")
        parser.parse(raw_html, meta)
    has_xml_warning = any(issubclass(warning.category, XMLParsedAsHTMLWarning) for warning in captured)
    return TestResult(name="XMLWarning suppressed", passed=not has_xml_warning)


def run_test_2_cda_text_complete(cda_rows: list[dict[str, str]]) -> TestResult:
    valid = True
    for row in cda_rows:
        text = row.get("cda_full_text", "").rstrip()
        if not text:
            valid = False
            break
        if text[-1] not in TERMINAL_CHARS:
            valid = False
            break
    return TestResult(
        name="CDA text complete",
        passed=valid,
        detail=f"({len(cda_rows)} rows checked)",
    )


def run_test_3_cda_token_count(cda_rows: list[dict[str, str]]) -> TestResult:
    all_above_threshold = True
    for row in cda_rows:
        raw_count = row.get("cda_token_count", "").strip()
        try:
            token_count = int(float(raw_count)) if raw_count else 0
        except ValueError:
            token_count = 0
        if token_count <= 500:
            all_above_threshold = False
            break
    return TestResult(name="CDA token count", passed=all_above_threshold)


def run_test_4_master_exists(master_path: Path, master_rows: list[dict[str, str]]) -> TestResult:
    return TestResult(
        name="Master CSV exists",
        passed=master_path.exists() and len(master_rows) > 0,
        detail=f"(rows={len(master_rows)})",
    )


def run_test_5_master_columns(master_fields: list[str]) -> TestResult:
    return TestResult(
        name="Master CSV columns",
        passed=MIN_MASTER_COLUMNS.issubset(set(master_fields)),
    )


def run_test_6_no_orphan_rows(
    log_rows: list[dict[str, str]],
    master_rows: list[dict[str, str]],
) -> TestResult:
    expected_pairs = {
        (row.get("cik", "").strip(), row.get("fiscal_year", "").strip())
        for row in log_rows
        if row.get("cik", "").strip() and row.get("fiscal_year", "").strip()
    }
    observed_pairs = {
        (row.get("cik", "").strip(), row.get("fiscal_year", "").strip())
        for row in master_rows
        if row.get("cik", "").strip() and row.get("fiscal_year", "").strip()
    }
    missing_pairs = expected_pairs - observed_pairs
    return TestResult(
        name="No orphan rows",
        passed=not missing_pairs,
        detail=f"(missing={len(missing_pairs)})",
    )


def run_test_7_numeric_sanity(master_fields: list[str], master_rows: list[dict[str, str]]) -> TestResult:
    negatives: list[tuple[str, str]] = []
    for column in NUMERIC_SANITY_COLUMNS:
        if column not in master_fields:
            continue
        for row in master_rows:
            value = _parse_number(row.get(column, ""))
            if value is not None and value < 0:
                negatives.append((column, row.get("accession_number", "")))
                break
    return TestResult(
        name="Numeric sanity",
        passed=not negatives,
        detail=f"(negative_columns={len(negatives)})",
    )


def run_test_8_status_check(log_rows: list[dict[str, str]]) -> tuple[TestResult, list[dict[str, str]]]:
    failures = [row for row in log_rows if row.get("status", "").strip().lower() != "success"]
    return (
        TestResult(
            name="Status check",
            passed=not failures,
            detail=f"(failures={len(failures)})",
        ),
        failures,
    )


def main() -> int:
    _set_csv_field_limit()
    cda_fields, cda_rows = _read_csv_rows(OUTPUT_DIR / "cda_full_text.csv")
    _ = cda_fields
    master_path = OUTPUT_DIR / "master_compensation.csv"
    master_fields, master_rows = _read_csv_rows(master_path)
    _, log_rows = _read_csv_rows(OUTPUT_DIR / "folder_ingest_log.csv")

    results: list[TestResult] = [
        run_test_1_xml_warning_suppressed(),
        run_test_2_cda_text_complete(cda_rows),
        run_test_3_cda_token_count(cda_rows),
        run_test_4_master_exists(master_path, master_rows),
        run_test_5_master_columns(master_fields),
        run_test_6_no_orphan_rows(log_rows, master_rows),
        run_test_7_numeric_sanity(master_fields, master_rows),
    ]
    test_8_result, status_failures = run_test_8_status_check(log_rows)
    results.append(test_8_result)

    for index, result in enumerate(results, start=1):
        suffix = f" {result.detail}" if result.detail else ""
        status = "PASS" if result.passed else "FAIL"
        print(f"TEST {index} - {result.name}: {status}{suffix}")

    if status_failures:
        print("Status failures:")
        for row in status_failures:
            print(
                f"- cik={row.get('cik','')} fiscal_year={row.get('fiscal_year','')} "
                f"accession={row.get('accession_number','')} status={row.get('status','')}"
            )

    return 0 if all(result.passed for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
