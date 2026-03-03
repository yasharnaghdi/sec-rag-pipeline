#!/usr/bin/env python3
"""Ingest DEF 14A filings from a client folder CSV and emit extraction outputs."""
from __future__ import annotations

import argparse
import csv
import logging
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, TextIO

from ingestion.cda_extractor import extract_cda
from ingestion.comp_table_extractor import (
    extract_equity_awards,
    extract_grants_plan_based,
    extract_option_exercises,
    extract_pension_benefits,
    extract_summary_compensation,
)
from ingestion.edgar_folder_fetcher import fetch_filing
from ingestion.metadata_model import DocumentMetadata
from ingestion.sec_chunker import SECChunker
from ingestion.sec_html_parser import SECHTMLParser

log = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
)

INPUT_CSV = Path("fixtures/client_input.csv")
OUTPUT_DIR = Path("output")

_META_FIELDS = [
    "folder_id",
    "file_number",
    "ticker",
    "company_name",
    "fiscal_year",
    "filing_date",
    "accession_number",
]

SUMMARY_COMP_FIELDS = _META_FIELDS + [
    "exec_name",
    "year",
    "salary",
    "bonus",
    "stock_awards",
    "option_awards",
    "non_equity_incentive",
    "pension_change",
    "other_comp",
    "total",
    "footnote_refs",
    "source_section",
    "table_block_id",
]
EQUITY_AWARDS_FIELDS = _META_FIELDS + [
    "exec_name",
    "option_grant_date",
    "options_unexercisable",
    "options_exercisable",
    "exercise_price",
    "expiration_date",
    "stock_awards_unvested_shares",
    "stock_awards_unvested_value",
    "footnote_refs",
    "source_section",
    "table_block_id",
]
GRANTS_FIELDS = _META_FIELDS + [
    "exec_name",
    "grant_date",
    "threshold",
    "target",
    "maximum",
    "shares_granted",
    "grant_fair_value",
    "footnote_refs",
    "source_section",
    "table_block_id",
]
OPTION_EX_FIELDS = _META_FIELDS + [
    "exec_name",
    "options_exercised",
    "options_value",
    "stock_vested_shares",
    "stock_vested_value",
    "footnote_refs",
    "source_section",
    "table_block_id",
]
PENSION_FIELDS = _META_FIELDS + [
    "exec_name",
    "plan_name",
    "years_credited",
    "present_value",
    "payments",
    "footnote_refs",
    "source_section",
    "table_block_id",
]
CDA_FIELDS = _META_FIELDS + [
    "cda_full_text",
    "cda_token_count",
    "pay_for_performance_flag",
    "cda_section_found",
]
LOG_FIELDS = _META_FIELDS + [
    "status",
    "summary_rows",
    "equity_rows",
    "grants_rows",
    "option_ex_rows",
    "pension_rows",
    "cda_tokens",
    "chunk_count",
    "block_count",
    "elapsed_seconds",
    "flag",
    "timestamp",
]

_OUTPUT_MAP: dict[str, list[str]] = {
    "comp_summary_table.csv": SUMMARY_COMP_FIELDS,
    "equity_awards_table.csv": EQUITY_AWARDS_FIELDS,
    "grants_plan_based.csv": GRANTS_FIELDS,
    "option_exercises_vested.csv": OPTION_EX_FIELDS,
    "pension_benefits.csv": PENSION_FIELDS,
    "cda_full_text.csv": CDA_FIELDS,
    "folder_ingest_log.csv": LOG_FIELDS,
}


def _string_value(row: dict[str, str], key: str) -> str:
    return row.get(key, "").strip()


def _extract_digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def _resolve_cik(row: dict[str, str], folder_id: str) -> str:
    provided = _string_value(row, "file_number") or _string_value(row, "cik")
    provided_digits = _extract_digits(provided)
    if provided_digits:
        return provided_digits

    folder_digits = _extract_digits(folder_id)
    if len(folder_digits) >= 18:
        return folder_digits[:10]
    return ""


def _read_rows(input_csv: Path) -> list[dict[str, str]]:
    with input_csv.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        return [
            {str(key): (value or "") for key, value in row.items()}
            for row in reader
            if row
        ]


def _open_writers(
    output_dir: Path,
) -> tuple[dict[str, csv.DictWriter[str]], dict[str, TextIO]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    handles: dict[str, TextIO] = {}
    writers: dict[str, csv.DictWriter[str]] = {}
    for filename, fields in _OUTPUT_MAP.items():
        path = output_dir / filename
        is_new = not path.exists()
        handle = path.open("a", newline="", encoding="utf-8")
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        if is_new:
            writer.writeheader()
        handles[filename] = handle
        writers[filename] = writer
    return writers, handles


def _write_success_log(
    writers: dict[str, csv.DictWriter[str]],
    meta: dict[str, Any],
    *,
    summary_rows: int,
    equity_rows: int,
    grants_rows: int,
    option_rows: int,
    pension_rows: int,
    cda_tokens: int,
    chunk_count: int,
    block_count: int,
    elapsed_seconds: float,
) -> None:
    writers["folder_ingest_log.csv"].writerow(
        {
            **meta,
            "status": "success",
            "summary_rows": summary_rows,
            "equity_rows": equity_rows,
            "grants_rows": grants_rows,
            "option_ex_rows": option_rows,
            "pension_rows": pension_rows,
            "cda_tokens": cda_tokens,
            "chunk_count": chunk_count,
            "block_count": block_count,
            "elapsed_seconds": round(elapsed_seconds, 2),
            "flag": "",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )


def _write_failed_log(
    writers: dict[str, csv.DictWriter[str]],
    meta: dict[str, Any],
    *,
    elapsed_seconds: float,
    flag: str,
) -> None:
    writers["folder_ingest_log.csv"].writerow(
        {
            **meta,
            "status": "failed",
            "summary_rows": 0,
            "equity_rows": 0,
            "grants_rows": 0,
            "option_ex_rows": 0,
            "pension_rows": 0,
            "cda_tokens": 0,
            "chunk_count": 0,
            "block_count": 0,
            "elapsed_seconds": round(elapsed_seconds, 2),
            "flag": flag[:200],
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )


def main() -> None:
    """Run folder-based ingest flow from CSV to output CSV artifacts."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=INPUT_CSV)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT_DIR)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    args = parser.parse_args()

    if not args.input.exists():
        sys.exit(f"ERROR: {args.input} not found")

    rows = _read_rows(args.input)
    if args.limit > 0:
        rows = rows[: args.limit]

    if args.dry_run:
        log.info("DRY RUN - %s rows to process:", len(rows))
        for row in rows:
            folder_id = _string_value(row, "folder_id")
            file_number = _string_value(row, "file_number") or _string_value(row, "cik")
            log.info(
                "  folder_id=%s  file_number=%s  ticker=%s  fiscal_year=%s",
                folder_id,
                file_number,
                _string_value(row, "ticker"),
                _string_value(row, "fiscal_year"),
            )
        return

    writers, handles = _open_writers(args.output_dir)
    html_parser = SECHTMLParser()
    chunker = SECChunker()

    try:
        for index, row in enumerate(rows, start=1):
            folder_id = _string_value(row, "folder_id")
            cik = _resolve_cik(row, folder_id)
            ticker = _string_value(row, "ticker")
            company_name = _string_value(row, "company_name")
            fiscal_year = _string_value(row, "fiscal_year")

            meta: dict[str, Any] = {
                "folder_id": folder_id,
                "file_number": cik,
                "ticker": ticker,
                "company_name": company_name,
                "fiscal_year": fiscal_year,
                "filing_date": "",
                "accession_number": "",
            }

            log.info("[%s/%s] folder=%s cik=%s ticker=%s", index, len(rows), folder_id, cik, ticker)
            t0 = time.perf_counter()

            if not folder_id or not cik:
                elapsed = time.perf_counter() - t0
                flag = "missing folder_id or cik/file_number"
                log.error("  x FAILED: %s", flag)
                _write_failed_log(writers, meta, elapsed_seconds=elapsed, flag=flag)
                continue

            try:
                fetched = fetch_filing(cik=cik, folder_id=folder_id)
                filing_date = fetched.filing_date or date.today()
                accession_number = fetched.accession_number
                meta["filing_date"] = filing_date.isoformat()
                meta["accession_number"] = accession_number

                document_id = f"{cik}_{accession_number.replace('-', '')}"
                doc_meta = DocumentMetadata(
                    document_id=document_id,
                    cik=cik,
                    company_name=company_name or ticker or cik,
                    form_type="DEF 14A",
                    filing_date=filing_date,
                    accession_number=accession_number,
                    source_url=fetched.filing_url,
                    fiscal_year_end=None,
                    raw_html_path=str(fetched.cache_path),
                )

                blocks = html_parser.parse(fetched.raw_html, doc_meta)
                chunks = chunker.chunk_blocks(blocks, doc_meta)

                summary_rows = extract_summary_compensation(blocks, meta)
                equity_rows = extract_equity_awards(blocks, meta)
                grants_rows = extract_grants_plan_based(blocks, meta)
                option_rows = extract_option_exercises(blocks, meta)
                pension_rows = extract_pension_benefits(blocks, meta)
                cda_row = extract_cda(blocks, meta)

                for result in summary_rows:
                    writers["comp_summary_table.csv"].writerow(result)
                for result in equity_rows:
                    writers["equity_awards_table.csv"].writerow(result)
                for result in grants_rows:
                    writers["grants_plan_based.csv"].writerow(result)
                for result in option_rows:
                    writers["option_exercises_vested.csv"].writerow(result)
                for result in pension_rows:
                    writers["pension_benefits.csv"].writerow(result)
                writers["cda_full_text.csv"].writerow(cda_row)

                for handle in handles.values():
                    handle.flush()

                elapsed = time.perf_counter() - t0
                _write_success_log(
                    writers,
                    meta,
                    summary_rows=len(summary_rows),
                    equity_rows=len(equity_rows),
                    grants_rows=len(grants_rows),
                    option_rows=len(option_rows),
                    pension_rows=len(pension_rows),
                    cda_tokens=int(cda_row.get("cda_token_count", 0)),
                    chunk_count=len(chunks),
                    block_count=len(blocks),
                    elapsed_seconds=elapsed,
                )
                log.info(
                    "  ok summary=%s equity=%s grants=%s option=%s pension=%s cda_tokens=%s",
                    len(summary_rows),
                    len(equity_rows),
                    len(grants_rows),
                    len(option_rows),
                    len(pension_rows),
                    cda_row.get("cda_token_count", 0),
                )
            except Exception as exc:
                elapsed = time.perf_counter() - t0
                log.error("  x FAILED: %s", exc)
                _write_failed_log(writers, meta, elapsed_seconds=elapsed, flag=str(exc))
    finally:
        for handle in handles.values():
            handle.close()

    log.info("Done. Outputs in %s", args.output_dir)


if __name__ == "__main__":
    main()
