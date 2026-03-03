#!/usr/bin/env python3
"""Ingest DEF 14A filings from client_input.csv (single column: folder_id = CIK)."""
from __future__ import annotations

import argparse
import csv
import logging
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, TextIO

import ingestion.edgar_folder_fetcher as edgar_folder_fetcher
from ingestion.cda_extractor import extract_cda
from ingestion.comp_table_extractor import (
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


@dataclass(frozen=True)
class _MasterSource:
    filename: str
    prefix: str
    has_exec_name: bool


_MASTER_SOURCES: tuple[_MasterSource, ...] = (
    _MasterSource(filename="comp_summary_table.csv", prefix="summary", has_exec_name=True),
    _MasterSource(filename="equity_awards_table.csv", prefix="equity", has_exec_name=True),
    _MasterSource(filename="grants_plan_based.csv", prefix="grants", has_exec_name=True),
    _MasterSource(filename="option_exercises_vested.csv", prefix="option_ex", has_exec_name=True),
    _MasterSource(filename="pension_benefits.csv", prefix="pension", has_exec_name=True),
    _MasterSource(filename="cda_full_text.csv", prefix="cda", has_exec_name=False),
)
_MASTER_KEY_FIELDS = ("cik", "fiscal_year", "exec_name")
_MASTER_BASE_FIELDS = _META_FIELDS + ["exec_name"]
_COMPANY_LEVEL_EXEC = "COMPANY_LEVEL"


def _open_writers(output_dir: Path) -> tuple[dict[str, csv.DictWriter[str]], dict[str, TextIO]]:
    output_dir.mkdir(parents=True, exist_ok=True)
    handles: dict[str, TextIO] = {}
    writers: dict[str, csv.DictWriter[str]] = {}

    for filename, fields in _OUTPUT_SCHEMAS.items():
        path = output_dir / filename
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

    fetch_single = getattr(edgar_folder_fetcher, "fetch_filing", None)
    if callable(fetch_single):
        return [fetch_single(cik=cik, folder_id=cik, form_type="DEF 14A")]

    msg = "No supported DEF 14A fetch function found in ingestion.edgar_folder_fetcher"
    raise AttributeError(msg)


def _configure_csv_field_limit() -> None:
    """Raise CSV parser field limit for very large CD&A text cells."""
    try:
        csv.field_size_limit(50_000_000)
    except OverflowError:
        csv.field_size_limit(sys.maxsize)


def _clean_csv_value(value: Any) -> str:
    if value is None:
        return ""
    return str(value).replace("\ufeff", "").strip()


def _normalize_exec_name(value: str) -> str:
    cleaned = _clean_csv_value(value)
    return cleaned if cleaned else _COMPANY_LEVEL_EXEC


def _set_if_missing(target: dict[str, str], field: str, value: str) -> None:
    if value and not _clean_csv_value(target.get(field, "")):
        target[field] = value


def _read_master_source(
    source_path: Path,
    *,
    has_exec_name: bool,
) -> tuple[list[str], dict[tuple[str, str, str], dict[str, str]]]:
    if not source_path.exists():
        return [], {}

    grouped: dict[tuple[str, str, str], dict[str, str]] = {}
    with source_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = [field for field in (reader.fieldnames or [])]

        for row in reader:
            cik = _clean_csv_value(row.get("cik"))
            fiscal_year = _clean_csv_value(row.get("fiscal_year"))
            if not cik and not fiscal_year:
                continue

            raw_exec_name = _clean_csv_value(row.get("exec_name")) if has_exec_name else _COMPANY_LEVEL_EXEC
            exec_name = _normalize_exec_name(raw_exec_name)
            key = (cik, fiscal_year, exec_name)
            aggregated = grouped.setdefault(key, {})
            aggregated["exec_name"] = exec_name

            for field in _META_FIELDS:
                _set_if_missing(aggregated, field, _clean_csv_value(row.get(field)))

            for field in fieldnames:
                if field in _META_FIELDS or field == "exec_name":
                    continue
                _set_if_missing(aggregated, field, _clean_csv_value(row.get(field)))

    return fieldnames, grouped


def _build_column_map(
    source_fields: list[str],
    *,
    prefix: str,
    used_fields: set[str],
) -> dict[str, str]:
    column_map: dict[str, str] = {}

    for field in source_fields:
        if field in _META_FIELDS or field == "exec_name":
            continue

        output_field = field
        if output_field in used_fields:
            output_field = f"{prefix}_{field}"
        while output_field in used_fields:
            output_field = f"{prefix}_{output_field}"

        column_map[field] = output_field
        used_fields.add(output_field)

    return column_map


def _row_count(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open(encoding="utf-8", newline="") as handle:
        return max(0, sum(1 for _ in handle) - 1)


def _build_master_compensation(output_dir: Path) -> tuple[Path, int]:
    """Build output/master_compensation.csv from topic CSV outputs."""
    _configure_csv_field_limit()
    master_rows: dict[tuple[str, str, str], dict[str, str]] = {}
    used_fields = set(_MASTER_BASE_FIELDS)
    output_fields = list(_MASTER_BASE_FIELDS)

    for source in _MASTER_SOURCES:
        source_path = output_dir / source.filename
        source_fields, grouped_rows = _read_master_source(
            source_path,
            has_exec_name=source.has_exec_name,
        )
        column_map = _build_column_map(
            source_fields,
            prefix=source.prefix,
            used_fields=used_fields,
        )
        output_fields.extend(column_map.values())

        for key, source_row in grouped_rows.items():
            row = master_rows.setdefault(
                key,
                {
                    "cik": key[0],
                    "company_name": "",
                    "ticker": "",
                    "fiscal_year": key[1],
                    "filing_date": "",
                    "accession_number": "",
                    "exec_name": key[2],
                },
            )

            for identity_field in _META_FIELDS:
                if identity_field == "cik":
                    row["cik"] = key[0]
                    continue
                if identity_field == "fiscal_year":
                    row["fiscal_year"] = key[1]
                    continue
                _set_if_missing(row, identity_field, _clean_csv_value(source_row.get(identity_field)))

            for source_field, output_field in column_map.items():
                _set_if_missing(row, output_field, _clean_csv_value(source_row.get(source_field)))

    master_path = output_dir / "master_compensation.csv"
    sorted_rows = [master_rows[key] for key in sorted(master_rows.keys())]
    with master_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=output_fields, extrasaction="ignore")
        writer.writeheader()
        for row in sorted_rows:
            writer.writerow({field: row.get(field, "") for field in output_fields})

    return master_path, len(sorted_rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0, help="Max CIKs to process")
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

    if not INPUT_CSV.exists():
        sys.exit(f"ERROR: {INPUT_CSV} not found")

    cik_list = _read_ciks(INPUT_CSV)
    if args.limit > 0:
        cik_list = cik_list[: args.limit]

    if args.build_master_only:
        master_path, master_count = _build_master_compensation(OUTPUT_DIR)
        log.info("Master CSV rebuilt: %s (rows=%s)", master_path, master_count)
        return

    log.info("CIKs to process: %s  (years_back=%s)", len(cik_list), args.years_back)

    if args.dry_run:
        for cik in cik_list:
            log.info("  CIK: %s", cik)
        return

    already_done = _load_processed_accessions(OUTPUT_DIR)
    writers, handles = _open_writers(OUTPUT_DIR)
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
                    "ticker": "",
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
    master_path, master_count = _build_master_compensation(OUTPUT_DIR)
    log.info("Master CSV: %s (rows=%s)", master_path, master_count)
    log.info("Outputs in %s/", OUTPUT_DIR)


if __name__ == "__main__":
    main()
