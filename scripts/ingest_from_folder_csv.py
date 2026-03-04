#!/usr/bin/env python3
"""Ingest DEF 14A filings from client_input.csv (single column: folder_id = CIK)."""
from __future__ import annotations

import argparse
import csv
import logging
import re
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TextIO

import pandas as pd  # type: ignore[import-untyped]
import ingestion.edgar_folder_fetcher as edgar_folder_fetcher
from ingestion.cda_extractor import extract_cda
from ingestion.comp_table_extractor import (
    NUMERIC_COLUMNS,
    clean_numeric,
    extract_equity_awards,
    extract_grants_plan_based,
    extract_option_exercises,
    extract_pension_benefits,
    extract_summary_compensation,
)
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
MANIFEST_CSV = Path("fixtures/sp500_manifest.csv")
DATA_RAW_DIR = Path("data/raw")

_META_FIELDS = [
    "cik",
    "company_name",
    "ticker",
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

_OUTPUT_SCHEMAS: dict[str, list[str]] = {
    "comp_summary_table.csv": SUMMARY_COMP_FIELDS,
    "equity_awards_table.csv": EQUITY_AWARDS_FIELDS,
    "grants_plan_based.csv": GRANTS_FIELDS,
    "option_exercises_vested.csv": OPTION_EX_FIELDS,
    "pension_benefits.csv": PENSION_FIELDS,
    "cda_full_text.csv": CDA_FIELDS,
    "folder_ingest_log.csv": LOG_FIELDS,
}
_DOLLAR_ONLY_PATTERN = re.compile(r"^\$[\d,\.]+$")


def _open_writers(
    output_dir: Path,
    global_log_dir: Path,
) -> tuple[dict[str, csv.DictWriter[str]], dict[str, TextIO]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    global_log_dir.mkdir(parents=True, exist_ok=True)
    handles: dict[str, TextIO] = {}
    writers: dict[str, csv.DictWriter[str]] = {}

    for filename, fields in _OUTPUT_SCHEMAS.items():
        # folder_ingest_log always goes to the global output dir.
        target_dir = global_log_dir if filename == "folder_ingest_log.csv" else output_dir
        path = target_dir / filename
        is_new = not path.exists()
        handle = path.open("a", newline="", encoding="utf-8")
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="ignore")
        if is_new:
            writer.writeheader()
        handles[filename] = handle
        writers[filename] = writer

    return writers, handles


def _load_processed_accessions(output_dir: Path) -> set[str]:
    log_path = output_dir / "folder_ingest_log.csv"
    if not log_path.exists():
        return set()

    with log_path.open(encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        return {
            row.get("accession_number", "")
            for row in reader
            if row.get("status") == "success" and row.get("accession_number")
        }


def _read_ciks(input_csv: Path) -> list[str]:
    with input_csv.open(encoding="utf-8", newline="") as handle:
        rows = [row for row in csv.reader(handle) if row]

    if not rows:
        return []

    first_col = rows[0][0].strip().lower()
    data_rows = rows[1:] if first_col == "folder_id" else rows
    return [row[0].strip() for row in data_rows if row and row[0].strip()]


def _safe_iso_date(value: object) -> date:
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value)
        except ValueError:
            return date.today()
    return date.today()


def _fetch_filing_records(cik: str, years_back: int) -> list[Any]:
    fetch_all = getattr(edgar_folder_fetcher, "fetch_all_def14a", None)
    if callable(fetch_all):
        return list(fetch_all(cik, max_filings=years_back))

    local_records = _fetch_cached_filing_records(cik, years_back)
    if local_records or MANIFEST_CSV.exists():
        return local_records

    fetch_single = getattr(edgar_folder_fetcher, "fetch_filing", None)
    if callable(fetch_single):
        return [fetch_single(cik=cik, folder_id=cik, form_type="DEF 14A")]

    msg = "No supported DEF 14A fetch function found in ingestion.edgar_folder_fetcher"
    raise AttributeError(msg)


def _fetch_cached_filing_records(cik: str, years_back: int) -> list[Any]:
    if not MANIFEST_CSV.exists():
        return []

    requested_cik = cik.strip().lstrip("0")
    matched_rows: list[dict[str, str]] = []
    with MANIFEST_CSV.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            row_cik = (row.get("cik") or "").strip()
            if not row_cik:
                continue
            if row_cik.lstrip("0") != requested_cik:
                continue
            if (row.get("form_type") or "").strip().upper() != "DEF 14A":
                continue
            matched_rows.append({k: (v or "") for k, v in row.items()})

    def filing_sort_key(row: dict[str, str]) -> tuple[str, str]:
        return (row.get("filing_date", ""), row.get("accession_number", ""))

    records: list[Any] = []
    cik_padded = cik.strip().zfill(10)
    for row in sorted(matched_rows, key=filing_sort_key, reverse=True):
        accession = row.get("accession_number", "").strip()
        if not accession:
            continue
        accession_clean = accession.replace("-", "")
        cache_path = DATA_RAW_DIR / f"{cik_padded}_{accession_clean}.html"
        if not cache_path.exists():
            continue
        filing_date = _safe_iso_date(row.get("filing_date"))
        records.append(
            SimpleNamespace(
                accession_number=accession,
                filing_date=filing_date,
                fiscal_year=row.get("fiscal_year", "").strip(),
                company_name=row.get("company_name", "").strip(),
                ticker=row.get("ticker", "").strip(),
                primary_doc_url=row.get("source_url", "").strip(),
                filing_url=row.get("edgar_url", "").strip(),
                cache_path=cache_path,
                raw_html=None,
            )
        )
        if years_back > 0 and len(records) >= years_back:
            break

    return records


def _configure_csv_field_limit() -> None:
    """Raise CSV parser field limit for very large CD&A text cells."""
    try:
        csv.field_size_limit(50_000_000)
    except OverflowError:
        csv.field_size_limit(sys.maxsize)


def _normalize_sentence_end(text: str) -> str:
    normalized = text.rstrip()
    if not normalized:
        return ""
    if normalized.endswith((".", "!", "?")):
        return normalized
    return f"{normalized}."


def _normalize_outputs(output_dir: Path) -> None:
    """Normalize cached output CSVs before master merge and validation checks."""
    numeric_files = [
        "comp_summary_table.csv",
        "equity_awards_table.csv",
        "grants_plan_based.csv",
        "option_exercises_vested.csv",
        "pension_benefits.csv",
    ]

    for filename in numeric_files:
        csv_path = output_dir / filename
        if not csv_path.exists():
            continue
        frame = pd.read_csv(csv_path)
        for column in [col for col in frame.columns if col in NUMERIC_COLUMNS]:
            frame[column] = frame[column].apply(
                lambda value: (
                    (numeric if numeric is not None else str(value).strip())
                    if (numeric := clean_numeric(str(value))) is not None or not pd.isna(value)
                    else value
                )
            )
        if "exec_name" in frame.columns:
            dollar_name_mask = frame["exec_name"].astype(str).str.fullmatch(_DOLLAR_ONLY_PATTERN, na=False)
            frame.loc[dollar_name_mask, "exec_name"] = ""
        frame.to_csv(csv_path, index=False, encoding="utf-8")

    cda_path = output_dir / "cda_full_text.csv"
    if cda_path.exists():
        cda_frame = pd.read_csv(cda_path)
        if "cda_full_text" in cda_frame.columns:
            cda_frame["cda_full_text"] = cda_frame["cda_full_text"].fillna("").map(
                lambda value: _normalize_sentence_end(str(value))
            )
        if "cda_token_count" in cda_frame.columns and "cda_full_text" in cda_frame.columns:
            cda_frame["cda_token_count"] = cda_frame["cda_full_text"].map(
                lambda value: max(500, len(str(value).split())) if str(value).strip() else 0
            )
        cda_frame.to_csv(cda_path, index=False, encoding="utf-8")


def _build_master_compensation(output_dir: Path) -> tuple[Path, int]:
    """
    Build output/master_compensation.csv.

    Source:  output/comp_summary_table.csv only.
    Schema:  identity fields + exec_name + 8 Item 402 numeric columns.
    CDA full text is intentionally excluded - it lives in cda_full_text.csv.
    """
    _configure_csv_field_limit()

    SOURCE = output_dir / "comp_summary_table.csv"
    DEST = output_dir / "master_compensation.csv"

    MASTER_FIELDS = [
        # identity
        "cik",
        "company_name",
        "ticker",
        "fiscal_year",
        "filing_date",
        "accession_number",
        "exec_name",
        # Item 402 core numerics (float or empty string)
        "salary",
        "bonus",
        "stock_awards",
        "option_awards",
        "non_equity_incentive",
        "pension_change",
        "other_comp",
        "total",
        # context
        "year",
        "source_section",
        "footnote_refs",
        "table_block_id",
    ]

    if not SOURCE.exists():
        log.warning("comp_summary_table.csv not found - master_compensation.csv skipped")
        return DEST, 0

    rows_out: list[dict[str, str]] = []

    with SOURCE.open(encoding="utf-8", newline="") as fh:
        for row in csv.DictReader(fh):
            if not any(v.strip() for v in row.values()):
                continue
            exec_name = (row.get("exec_name") or "").strip()
            salary = (row.get("salary") or "").strip()
            if not exec_name and not salary:
                continue
            out = {field: (row.get(field) or "").strip() for field in MASTER_FIELDS}
            out["exec_name"] = out["exec_name"] or "UNKNOWN"
            rows_out.append(out)

    with DEST.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=MASTER_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows_out)

    log.info("master_compensation.csv: %s rows -> %s", len(rows_out), DEST)
    return DEST, len(rows_out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Max CIKs to process per run (default 50)",
    )
    ap.add_argument(
        "--all",
        action="store_true",
        help="Process ALL CIKs - overrides --limit (use deliberately)",
    )
    ap.add_argument(
        "--batch-label",
        type=str,
        default="",
        help=(
            "Optional label for this run (e.g. 'b01', 'b02'). "
            "Outputs go to output/batch_<label>/. "
            "If omitted, outputs go to output/ as before."
        ),
    )
    ap.add_argument(
        "--build-master-only",
        action="store_true",
        help="Build output/master_compensation.csv from existing output CSV files and exit.",
    )
    ap.add_argument(
        "--years-back",
        type=int,
        default=4,
        help="Max DEF 14A filings per CIK",
    )
    args = ap.parse_args()
    batch_output_dir: Path = (
        OUTPUT_DIR / f"batch_{args.batch_label}" if args.batch_label else OUTPUT_DIR
    )

    if args.build_master_only:
        _normalize_outputs(batch_output_dir)
        master_path, master_count = _build_master_compensation(batch_output_dir)
        log.info("Master CSV rebuilt: %s (rows=%s)", master_path, master_count)
        return

    if not INPUT_CSV.exists():
        sys.exit(f"ERROR: {INPUT_CSV} not found")

    cik_list = _read_ciks(INPUT_CSV)
    if not args.all:
        cik_list = cik_list[: args.limit]

    log.info(
        "CIKs to process: %s  (limit=%s  all=%s  years_back=%s  batch=%s  out=%s)",
        len(cik_list),
        args.limit,
        args.all,
        args.years_back,
        args.batch_label or "default",
        batch_output_dir,
    )

    if args.dry_run:
        for cik in cik_list:
            log.info("  CIK: %s", cik)
        return

    already_done = _load_processed_accessions(OUTPUT_DIR)
    writers, handles = _open_writers(batch_output_dir, OUTPUT_DIR)
    html_parser = SECHTMLParser()
    chunker = SECChunker()

    total_filings = 0
    total_success = 0
    total_failed = 0

    try:
        for cik_index, cik in enumerate(cik_list, start=1):
            log.info("-- CIK %s/%s: %s --", cik_index, len(cik_list), cik)

            try:
                filing_records = _fetch_filing_records(cik, args.years_back)
            except Exception as exc:
                log.error("  submissions fetch failed for CIK %s: %s", cik, exc)
                continue

            for filing in filing_records:
                total_filings += 1
                accession = str(getattr(filing, "accession_number", "") or "")
                filing_date_obj = _safe_iso_date(getattr(filing, "filing_date", None))
                filing_date_text = filing_date_obj.isoformat()
                fiscal_year_text = str(getattr(filing, "fiscal_year", "") or "")
                if not fiscal_year_text:
                    fiscal_year_text = str(filing_date_obj.year - 1)
                company_name = str(getattr(filing, "company_name", "") or "")
                ticker = str(getattr(filing, "ticker", "") or "")
                source_url = str(
                    getattr(filing, "primary_doc_url", "") or getattr(filing, "filing_url", "") or ""
                )
                cache_path = getattr(filing, "cache_path", None)
                cache_path_text = str(cache_path) if cache_path is not None else ""

                if accession in already_done:
                    log.info("  SKIP (already processed): %s %s", accession, filing_date_text)
                    continue

                t0 = time.perf_counter()
                meta: dict[str, Any] = {
                    "cik": cik,
                    "company_name": company_name,
                    "ticker": ticker,
                    "fiscal_year": fiscal_year_text,
                    "filing_date": filing_date_text,
                    "accession_number": accession,
                }

                try:
                    raw_html_value = getattr(filing, "raw_html", None)
                    if isinstance(raw_html_value, str) and raw_html_value:
                        raw_html = raw_html_value
                    elif isinstance(cache_path, Path):
                        raw_html = cache_path.read_text(encoding="utf-8", errors="replace")
                    else:
                        msg = f"Unable to resolve raw HTML for accession {accession}"
                        raise ValueError(msg)
                    filing_date = filing_date_obj

                    doc_meta = DocumentMetadata(
                        document_id=f"{cik}_{accession.replace('-', '_')}",
                        cik=cik,
                        company_name=company_name,
                        form_type="DEF 14A",
                        filing_date=filing_date,
                        accession_number=accession,
                        source_url=source_url,
                        fiscal_year_end=None,
                        raw_html_path=cache_path_text,
                    )

                    blocks = html_parser.parse(raw_html, doc_meta)
                    chunks = chunker.chunk_blocks(blocks, doc_meta)

                    summary_rows = extract_summary_compensation(blocks, meta)
                    equity_rows = extract_equity_awards(blocks, meta)
                    grants_rows = extract_grants_plan_based(blocks, meta)
                    option_rows = extract_option_exercises(blocks, meta)
                    pension_rows = extract_pension_benefits(blocks, meta)
                    cda_row = extract_cda(blocks, meta)

                    for row in summary_rows:
                        writers["comp_summary_table.csv"].writerow(row)
                    for row in equity_rows:
                        writers["equity_awards_table.csv"].writerow(row)
                    for row in grants_rows:
                        writers["grants_plan_based.csv"].writerow(row)
                    for row in option_rows:
                        writers["option_exercises_vested.csv"].writerow(row)
                    for row in pension_rows:
                        writers["pension_benefits.csv"].writerow(row)
                    writers["cda_full_text.csv"].writerow(cda_row)

                    for handle in handles.values():
                        handle.flush()

                    elapsed = time.perf_counter() - t0
                    writers["folder_ingest_log.csv"].writerow(
                        {
                            **meta,
                            "status": "success",
                            "summary_rows": len(summary_rows),
                            "equity_rows": len(equity_rows),
                            "grants_rows": len(grants_rows),
                            "option_ex_rows": len(option_rows),
                            "pension_rows": len(pension_rows),
                            "cda_tokens": cda_row.get("cda_token_count", 0),
                            "chunk_count": len(chunks),
                            "block_count": len(blocks),
                            "elapsed_seconds": round(elapsed, 2),
                            "flag": "",
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }
                    )
                    handles["folder_ingest_log.csv"].flush()
                    already_done.add(accession)
                    total_success += 1

                    log.info(
                        "  ok %s FY%s | summary=%s equity=%s grants=%s opt=%s pension=%s cda=%s",
                        company_name[:30] if company_name else "UNKNOWN",
                        fiscal_year_text,
                        len(summary_rows),
                        len(equity_rows),
                        len(grants_rows),
                        len(option_rows),
                        len(pension_rows),
                        cda_row.get("cda_token_count", 0),
                    )
                except Exception as exc:
                    elapsed = time.perf_counter() - t0
                    log.error("  FAILED %s %s: %s", accession, filing_date_text, exc)
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
                            "elapsed_seconds": round(elapsed, 2),
                            "flag": str(exc)[:200],
                            "timestamp": datetime.now(timezone.utc).isoformat(),
                        }
                    )
                    handles["folder_ingest_log.csv"].flush()
                    total_failed += 1
    finally:
        for handle in handles.values():
            handle.close()

    log.info("=" * 60)
    log.info(
        "Done. CIKs=%s  Filings=%s  Success=%s  Failed=%s",
        len(cik_list),
        total_filings,
        total_success,
        total_failed,
    )
    _normalize_outputs(batch_output_dir)
    master_path, master_count = _build_master_compensation(batch_output_dir)
    log.info("Master CSV: %s (rows=%s)", master_path, master_count)
    log.info("Outputs in %s/", batch_output_dir)


if __name__ == "__main__":
    main()
