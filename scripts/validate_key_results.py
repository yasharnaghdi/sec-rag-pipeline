"""Post-batch validator for output/<batch_label>/key_results.csv."""
from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

MIN_CEO_COVERAGE = 30
MIN_CDA_COVERAGE = 20

NUMERIC_FIELDS = [
    "ceo_salary",
    "ceo_bonus",
    "ceo_stock_awards",
    "ceo_option_awards",
    "ceo_total",
    "cfo_salary",
    "cfo_total",
    "coo_salary",
    "coo_total",
    "other1_salary",
    "other1_total",
    "other2_salary",
    "other2_total",
]

_NUMERIC_VALUE_RE = re.compile(r"^\d+(\.\d+)?$")
_YEAR_RE = re.compile(r"^\d{4}$")


def validate(input_path: Path, expected_rows: int) -> list[str]:
    """Run all validation checks and return failure messages."""
    failures: list[str] = []

    if not input_path.exists():
        return [f"File not found: {input_path}"]

    with input_path.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    actual_rows = len(rows)
    if actual_rows != expected_rows:
        failures.append(f"CHECK 1 FAIL: Expected {expected_rows} rows, got {actual_rows}")
    else:
        print(f"CHECK 1 PASS: Row count = {actual_rows}")

    ceo_total_populated = sum(1 for row in rows if row.get("ceo_total", "").strip())
    if ceo_total_populated < MIN_CEO_COVERAGE:
        failures.append(
            "CHECK 2 FAIL: CEO total populated for only "
            f"{ceo_total_populated}/{actual_rows} rows (minimum: {MIN_CEO_COVERAGE})"
        )
    else:
        print(f"CHECK 2 PASS: CEO total populated for {ceo_total_populated}/{actual_rows} rows")

    silent_ok = [
        row.get("cik", "")
        for row in rows
        if row.get("status") == "ok"
        and not row.get("ceo_name", "").strip()
        and not row.get("ceo_total", "").strip()
        and row.get("extraction_method", "") != "failed"
    ]
    if silent_ok:
        failures.append(
            "CHECK 3 FAIL: "
            f"{len(silent_ok)} rows have status=ok but empty ceo_name and ceo_total: "
            f"CIKs={silent_ok[:5]}"
        )
    else:
        print("CHECK 3 PASS: No silent-ok rows with empty CEO data")

    dirty_rows: list[tuple[str, str, str]] = []
    for row in rows:
        for field in NUMERIC_FIELDS:
            value = row.get(field, "").strip()
            if value and not _NUMERIC_VALUE_RE.match(value):
                dirty_rows.append((row.get("cik", "?"), field, value))

    if dirty_rows:
        failures.append(
            "CHECK 4 FAIL: "
            f"{len(dirty_rows)} cells contain non-numeric values in numeric columns. "
            f"Examples: {dirty_rows[:3]}"
        )
    else:
        print("CHECK 4 PASS: All numeric columns contain clean digit strings")

    bad_years = [
        (row.get("cik", "?"), row.get("fiscal_year", ""))
        for row in rows
        if row.get("fiscal_year", "").strip()
        and not _YEAR_RE.match(row.get("fiscal_year", "").strip())
    ]
    if bad_years:
        failures.append(
            "CHECK 5 FAIL: "
            f"{len(bad_years)} rows have malformed fiscal_year. Examples: {bad_years[:3]}"
        )
    else:
        print("CHECK 5 PASS: fiscal_year format OK where populated")

    cda_populated = sum(1 for row in rows if int(row.get("cda_token_count", 0) or 0) > 0)
    if cda_populated < MIN_CDA_COVERAGE:
        failures.append(
            "CHECK 6 FAIL: cda_token_count > 0 for only "
            f"{cda_populated}/{actual_rows} rows (minimum: {MIN_CDA_COVERAGE})"
        )
    else:
        print(f"CHECK 6 PASS: CD&A populated for {cda_populated}/{actual_rows} rows")

    return failures


def main() -> None:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(
        description="Validate key_results.csv from run_batch50_key_results.py"
    )
    parser.add_argument(
        "--input",
        default="output/b01/key_results.csv",
        help="Path to key_results.csv (default: output/b01/key_results.csv)",
    )
    parser.add_argument(
        "--expected-rows",
        type=int,
        default=50,
        help="Expected number of rows in the CSV (default: 50)",
    )
    args = parser.parse_args()

    print(f"\nValidating: {args.input}\n{'-' * 60}")
    failures = validate(Path(args.input), args.expected_rows)
    print(f"\n{'-' * 60}")

    if failures:
        print(f"\n{len(failures)} check(s) FAILED:\n")
        for failure in failures:
            print(f"  - {failure}")
        sys.exit(1)

    print("\nAll checks passed.")
    sys.exit(0)


if __name__ == "__main__":
    main()
