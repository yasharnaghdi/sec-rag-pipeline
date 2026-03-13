#!/usr/bin/env python
"""Run a small cached sample and print data-quality diagnostics."""
from __future__ import annotations

import argparse
import csv
import logging
import re
import sys
from datetime import date
from pathlib import Path

if __package__ in {None, ""}:
    project_root = Path(__file__).resolve().parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

from ingestion.edgar_folder_fetcher import FetchedFiling
from scripts.run_batch50_key_results import DEFAULT_MODEL, process_cik


def _load_ciks(input_path: Path, limit: int) -> list[str]:
    with input_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        fieldnames = reader.fieldnames or []
        cik_col = next(
            (column for column in fieldnames if column.lower() in {"cik", "folder_id"}),
            fieldnames[0] if fieldnames else "cik",
        )
        return [
            row[cik_col].strip()
            for row in reader
            if row.get(cik_col, "").strip()
        ][:limit]


def _parse_date(value: str) -> date | None:
    value = value.strip()
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _cached_filing_override(cik: str) -> FetchedFiling | None:
    reference_path = Path("output/b01/key_results.csv")
    if reference_path.exists():
        with reference_path.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if row.get("cik", "").strip() != cik:
                    continue
                accession = row.get("accession_number", "").strip()
                accession_digits = accession.replace("-", "")
                cache_path = Path("data/raw") / f"{cik.zfill(10)}_{accession_digits}.html"
                if not accession or not cache_path.exists():
                    break
                return FetchedFiling(
                    raw_html=cache_path.read_text(encoding="utf-8", errors="replace"),
                    accession_number=accession,
                    filing_date=_parse_date(row.get("filing_date", "")),
                    filing_url=row.get("filing_url", "").strip(),
                    cache_path=cache_path,
                    company_name=row.get("company_name", "").strip(),
                    ticker=row.get("ticker", "").strip(),
                )

    candidates = sorted(Path("data/raw").glob(f"{cik.zfill(10)}_*.html"))
    if not candidates:
        return None

    cache_path = candidates[-1]
    accession_digits = cache_path.stem.split("_", maxsplit=1)[1]
    accession_number = (
        f"{accession_digits[:10]}-{accession_digits[10:12]}-{accession_digits[12:]}"
        if len(accession_digits) >= 14
        else accession_digits
    )
    return FetchedFiling(
        raw_html=cache_path.read_text(encoding="utf-8", errors="replace"),
        accession_number=accession_number,
        filing_date=None,
        filing_url="",
        cache_path=cache_path,
        company_name="",
        ticker="",
    )


def main() -> int:
    logging.disable(logging.CRITICAL)

    parser = argparse.ArgumentParser(description="Run a 5-CIK data-quality diagnostic sample.")
    parser.add_argument(
        "--input",
        default="fixtures/client_input.csv",
        help="CSV file with a cik/folder_id column (default: fixtures/client_input.csv)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=5,
        help="Number of CIKs to sample (default: 5)",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"Fallback LLM model to pass through when needed (default: {DEFAULT_MODEL})",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    ciks = _load_ciks(input_path, args.limit)

    print(
        "cik,ceo_name,ceo_title,fiscal_year,ceo_total,status,extraction_method,"
        "has_summary_comp,has_cda,has_exec_comp"
    )

    invalid_rows = 0
    for cik in ciks:
        result_row, _, _, _ = process_cik(
            cik=cik,
            model=args.model,
            skip_db=True,
            filing_override=_cached_filing_override(cik),
        )
        print(
            ",".join(
                [
                    cik,
                    str(result_row.get("ceo_name", "")),
                    str(result_row.get("ceo_title", "")),
                    str(result_row.get("fiscal_year", "")),
                    str(result_row.get("ceo_total", "")),
                    str(result_row.get("status", "")),
                    str(result_row.get("extraction_method", "")),
                    str(result_row.get("has_summary_comp", "")),
                    str(result_row.get("has_cda", "")),
                    str(result_row.get("has_exec_comp", "")),
                ]
            )
        )

        status = str(result_row.get("status", "") or "")
        fiscal_year = str(result_row.get("fiscal_year", "") or "")
        ceo_name = str(result_row.get("ceo_name", "") or "").strip()
        ceo_total = str(result_row.get("ceo_total", "") or "").strip()
        if status == "ok" and (not ceo_name or not ceo_total or not re.fullmatch(r"\d{4}", fiscal_year)):
            invalid_rows += 1

    if invalid_rows == 0:
        print("\nNo year/empty-column regression reproduced in this cached sample.")
    else:
        print(f"\nDetected {invalid_rows} suspect row(s) in the cached sample.")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
