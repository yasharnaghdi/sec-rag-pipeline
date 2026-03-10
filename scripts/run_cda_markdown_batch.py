#!/usr/bin/env python3
"""Batch pipeline: CIK CSV -> multi-section markdown extraction outputs.

Usage:
    poetry run python scripts/run_cda_markdown_batch.py \
      --input fixtures/client_input.csv \
      --batch-label cda_b01 \
      --fiscal-year-start 2023 \
      --fiscal-year-end 2025
"""

from __future__ import annotations

import argparse
import csv
import logging
import sys
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

if __package__ in {None, ""}:
    project_root = Path(__file__).resolve().parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
else:
    project_root = Path(__file__).resolve().parents[1]

load_dotenv(project_root / ".env", override=False)

import ingestion.edgar_folder_fetcher as fetcher  # noqa: E402
from ingestion.cda_markdown_extractor import SECTION_NAMES, extract_section_markdown  # noqa: E402
from ingestion.metadata_model import DocumentMetadata  # noqa: E402

log = logging.getLogger(__name__)

DEFAULT_LIMIT = 50
DEFAULT_MANIFEST = Path("fixtures/sp500_manifest.csv")
DEFAULT_OUTPUT_BASE = Path("output")

CDA_OUTPUT_COLUMNS = [
    "cik",
    "company_name",
    "ticker",
    "filing_date",
    "fiscal_year",
    "section_name",
    "section_key",
    "accession_number",
    "filing_url",
    "raw_html_path",
    "start_anchor",
    "end_anchor",
    "start_page",
    "end_page",
    "strategy",
    "warnings",
    "confidence",
    "markdown_path",
    "markdown",
    "status",
    "error",
]

LOG_OUTPUT_COLUMNS = [
    "cik",
    "company_name",
    "accession_number",
    "filing_date",
    "fiscal_year",
    "section_name",
    "section_key",
    "status",
    "strategy",
    "confidence",
    "warnings",
    "error",
]


@dataclass
class FilingRecord:
    cik: str
    company_name: str
    ticker: str
    filing_date: date
    fiscal_year: int
    accession_number: str
    filing_url: str
    source_url: str
    raw_html_path: Path


def _parse_iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Invalid ISO date '{value}'. Expected YYYY-MM-DD.") from exc


def _read_ciks(input_path: Path) -> list[str]:
    with input_path.open(encoding="utf-8", newline="") as handle:
        rows = [row for row in csv.reader(handle) if row]

    if not rows:
        return []

    header = rows[0][0].strip().lower()
    data_rows = rows[1:] if header in {"cik", "folder_id"} else rows
    ciks = [row[0].strip() for row in data_rows if row and row[0].strip()]
    return ciks


def _normalize_cik(value: str) -> str:
    digits = "".join(char for char in value if char.isdigit())
    return digits or value.strip()


def _infer_fiscal_year_from_filing_date(filing_date: date) -> int:
    return filing_date.year - 1 if filing_date.month <= 8 else filing_date.year


def _parse_fiscal_year(value: str) -> int | None:
    cleaned = value.strip()
    if not cleaned.isdigit() or len(cleaned) != 4:
        return None
    return int(cleaned)


def _parse_manifest_records(manifest_path: Path, cik: str) -> list[FilingRecord]:
    if not manifest_path.exists():
        return []

    target_cik = _normalize_cik(cik).lstrip("0")
    records: list[FilingRecord] = []
    with manifest_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            row_cik = _normalize_cik(str(row.get("cik", ""))).lstrip("0")
            if not row_cik or row_cik != target_cik:
                continue
            form_type = str(row.get("form_type", "")).upper().replace(" ", "")
            if form_type != "DEF14A":
                continue

            filing_date_raw = str(row.get("filing_date", "")).strip()
            accession = str(row.get("accession_number", "")).strip()
            if not filing_date_raw or not accession:
                continue

            try:
                filing_date = _parse_iso_date(filing_date_raw)
            except ValueError:
                continue

            raw_html_path_raw = str(row.get("raw_html_path", "")).strip()
            if raw_html_path_raw:
                raw_html_path = Path(raw_html_path_raw)
            else:
                accession_compact = accession.replace("-", "")
                raw_html_path = Path("data/raw") / f"{_normalize_cik(cik).zfill(10)}_{accession_compact}.html"

            filing_url = str(row.get("edgar_url", "")).strip()
            source_url = str(row.get("source_url", "")).strip() or filing_url
            fiscal_year = _parse_fiscal_year(str(row.get("fiscal_year", "")))
            if fiscal_year is None:
                fiscal_year = _infer_fiscal_year_from_filing_date(filing_date)

            records.append(
                FilingRecord(
                    cik=_normalize_cik(cik).zfill(10),
                    company_name=str(row.get("company_name", "")).strip(),
                    ticker=str(row.get("ticker", "")).strip(),
                    filing_date=filing_date,
                    fiscal_year=fiscal_year,
                    accession_number=accession,
                    filing_url=filing_url,
                    source_url=source_url,
                    raw_html_path=raw_html_path,
                )
            )

    records.sort(key=lambda record: (record.filing_date, record.accession_number), reverse=True)
    deduped: list[FilingRecord] = []
    seen_accessions: set[str] = set()
    for record in records:
        accession_key = record.accession_number.strip()
        if accession_key in seen_accessions:
            continue
        seen_accessions.add(accession_key)
        deduped.append(record)
    return deduped


def _filter_by_fiscal_year(
    records: list[FilingRecord],
    fiscal_year_start: int | None,
    fiscal_year_end: int | None,
) -> list[FilingRecord]:
    if fiscal_year_start is None and fiscal_year_end is None:
        return records

    filtered: list[FilingRecord] = []
    for record in records:
        if fiscal_year_start is not None and record.fiscal_year < fiscal_year_start:
            continue
        if fiscal_year_end is not None and record.fiscal_year > fiscal_year_end:
            continue
        filtered.append(record)
    return filtered


def _load_raw_html(record: FilingRecord, allow_fetch_fallback: bool) -> tuple[str, FilingRecord]:
    if record.raw_html_path.exists():
        html = record.raw_html_path.read_text(encoding="utf-8", errors="replace")
        return html, record

    if not allow_fetch_fallback:
        msg = f"Raw filing HTML missing and fetch fallback disabled: {record.raw_html_path}"
        raise FileNotFoundError(msg)

    fetched = fetcher.fetch_filing(
        cik=record.cik,
        folder_id=record.accession_number,
        form_type="DEF 14A",
    )

    updated_record = FilingRecord(
        cik=record.cik,
        company_name=record.company_name or fetched.company_name,
        ticker=record.ticker or fetched.ticker,
        filing_date=record.filing_date,
        fiscal_year=record.fiscal_year,
        accession_number=record.accession_number,
        filing_url=record.filing_url or fetched.filing_url,
        source_url=record.source_url or fetched.filing_url,
        raw_html_path=fetched.cache_path,
    )
    return fetched.raw_html, updated_record


def _fetch_latest_if_needed(
    cik: str,
    fiscal_year_start: int | None,
    fiscal_year_end: int | None,
    allow_fetch_fallback: bool,
) -> list[FilingRecord]:
    if not allow_fetch_fallback:
        return []

    fetched = fetcher.fetch_latest_def14a(cik)
    filing_date = fetched.filing_date
    if filing_date is None:
        return []
    fiscal_year = _infer_fiscal_year_from_filing_date(filing_date)
    if fiscal_year_start is not None and fiscal_year < fiscal_year_start:
        return []
    if fiscal_year_end is not None and fiscal_year > fiscal_year_end:
        return []

    return [
        FilingRecord(
            cik=_normalize_cik(cik).zfill(10),
            company_name=fetched.company_name,
            ticker=fetched.ticker,
            filing_date=filing_date,
            fiscal_year=fiscal_year,
            accession_number=fetched.accession_number,
            filing_url=fetched.filing_url,
            source_url=fetched.filing_url,
            raw_html_path=fetched.cache_path,
        )
    ]


def _build_doc_metadata(record: FilingRecord) -> DocumentMetadata:
    return DocumentMetadata(
        document_id=f"{record.cik}_{record.accession_number.replace('-', '_')}",
        cik=record.cik,
        company_name=record.company_name or "",
        form_type="DEF 14A",
        filing_date=record.filing_date,
        accession_number=record.accession_number,
        source_url=record.source_url or record.filing_url,
        fiscal_year_end=None,
        raw_html_path=str(record.raw_html_path),
    )


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    parser = argparse.ArgumentParser(description="Batch pipeline: CIK list -> section_markdown.csv")
    parser.add_argument(
        "--input",
        default="fixtures/client_input.csv",
        help="CSV file with CIK values (header may be 'cik' or 'folder_id').",
    )
    parser.add_argument(
        "--batch-label",
        default="cda_b01",
        help="Output subfolder label under output/ (default: cda_b01).",
    )
    parser.add_argument(
        "--manifest",
        default=str(DEFAULT_MANIFEST),
        help=f"Manifest CSV with filing metadata (default: {DEFAULT_MANIFEST}).",
    )
    parser.add_argument(
        "--output-base",
        default=str(DEFAULT_OUTPUT_BASE),
        help=f"Base output directory (default: {DEFAULT_OUTPUT_BASE}).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Max CIKs to process (default: {DEFAULT_LIMIT}).",
    )
    parser.add_argument(
        "--max-filings-per-cik",
        type=int,
        default=10,
        help="Max filings to process per CIK after fiscal-year filtering (default: 10).",
    )
    parser.add_argument(
        "--fiscal-year-start",
        type=int,
        default=None,
        help=(
            "Inclusive start fiscal year for filtering. "
            "Must be paired with --fiscal-year-end."
        ),
    )
    parser.add_argument(
        "--fiscal-year-end",
        type=int,
        default=None,
        help=(
            "Inclusive end fiscal year for filtering. "
            "Must be paired with --fiscal-year-start."
        ),
    )
    parser.add_argument(
        "--no-fetch-fallback",
        action="store_true",
        help="Do not fetch from EDGAR when local manifest/cache data is missing.",
    )
    parser.add_argument(
        "--no-markdown-files",
        action="store_true",
        help="Do not write per-filing .md files; keep markdown only in CSV.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print planned CIK list and exit.",
    )
    args = parser.parse_args()

    has_fy_start = args.fiscal_year_start is not None
    has_fy_end = args.fiscal_year_end is not None
    if has_fy_start != has_fy_end:
        parser.error("Both --fiscal-year-start and --fiscal-year-end are required to enable fiscal-year filtering.")

    fiscal_year_start: int | None = None
    fiscal_year_end: int | None = None
    if has_fy_start and has_fy_end:
        assert args.fiscal_year_start is not None
        assert args.fiscal_year_end is not None
        if args.fiscal_year_start < 1000 or args.fiscal_year_start > 9999:
            parser.error("--fiscal-year-start must be a 4-digit year.")
        if args.fiscal_year_end < 1000 or args.fiscal_year_end > 9999:
            parser.error("--fiscal-year-end must be a 4-digit year.")
        if args.fiscal_year_start > args.fiscal_year_end:
            parser.error("--fiscal-year-start cannot be greater than --fiscal-year-end.")
        fiscal_year_start = args.fiscal_year_start
        fiscal_year_end = args.fiscal_year_end

    input_path = Path(args.input)
    manifest_path = Path(args.manifest)
    output_base = Path(args.output_base)

    if not input_path.exists():
        log.error("Input file not found: %s", input_path)
        sys.exit(1)

    ciks = _read_ciks(input_path)[: args.limit]
    if not ciks:
        log.error("No CIK values found in input: %s", input_path)
        sys.exit(1)

    log.info(
        "batch start | label=%s ciks=%d fiscal_year_range=%s..%s",
        args.batch_label,
        len(ciks),
        str(fiscal_year_start) if fiscal_year_start is not None else "min",
        str(fiscal_year_end) if fiscal_year_end is not None else "max",
    )

    if args.dry_run:
        for cik in ciks:
            log.info("cik=%s", cik)
        return

    out_dir = output_base / args.batch_label
    markdown_dir = out_dir / "cda_markdown"
    out_dir.mkdir(parents=True, exist_ok=True)
    if not args.no_markdown_files:
        markdown_dir.mkdir(parents=True, exist_ok=True)

    cda_csv_path = out_dir / "cda_markdown.csv"
    log_csv_path = out_dir / "cda_markdown_log.csv"

    success_count = 0
    failure_count = 0

    with (
        cda_csv_path.open("w", encoding="utf-8", newline="") as cda_file,
        log_csv_path.open("w", encoding="utf-8", newline="") as log_file,
    ):
        cda_writer = csv.DictWriter(cda_file, fieldnames=CDA_OUTPUT_COLUMNS, extrasaction="ignore")
        run_log_writer = csv.DictWriter(log_file, fieldnames=LOG_OUTPUT_COLUMNS, extrasaction="ignore")
        cda_writer.writeheader()
        run_log_writer.writeheader()

        for index, cik in enumerate(ciks, start=1):
            cik_padded = _normalize_cik(cik).zfill(10)
            log.info("[%d/%d] processing cik=%s", index, len(ciks), cik_padded)

            records = _parse_manifest_records(manifest_path, cik_padded)
            records = _filter_by_fiscal_year(records, fiscal_year_start, fiscal_year_end)

            if not records:
                try:
                    records = _fetch_latest_if_needed(
                        cik=cik_padded,
                        fiscal_year_start=fiscal_year_start,
                        fiscal_year_end=fiscal_year_end,
                        allow_fetch_fallback=not args.no_fetch_fallback,
                    )
                except Exception as exc:  # noqa: BLE001
                    failure_count += 1
                    error_message = str(exc)
                    run_log_writer.writerow(
                        {
                            "cik": cik_padded,
                            "company_name": "",
                            "accession_number": "",
                            "filing_date": "",
                            "fiscal_year": "",
                            "section_name": "",
                            "section_key": "",
                            "status": "failed",
                            "strategy": "",
                            "confidence": "",
                            "warnings": "",
                            "error": error_message,
                        }
                    )
                    cda_writer.writerow(
                        {
                            "cik": cik_padded,
                            "company_name": "",
                            "ticker": "",
                            "filing_date": "",
                            "fiscal_year": "",
                            "section_name": "",
                            "section_key": "",
                            "accession_number": "",
                            "filing_url": "",
                            "raw_html_path": "",
                            "start_anchor": "",
                            "end_anchor": "",
                            "start_page": "",
                            "end_page": "",
                            "strategy": "",
                            "warnings": "",
                            "confidence": "",
                            "markdown_path": "",
                            "markdown": "",
                            "status": "failed",
                            "error": error_message,
                        }
                    )
                    continue

            if not records:
                failure_count += 1
                message = "No filings found for CIK in the selected fiscal-year range."
                run_log_writer.writerow(
                    {
                        "cik": cik_padded,
                        "company_name": "",
                        "accession_number": "",
                        "filing_date": "",
                        "fiscal_year": "",
                        "section_name": "",
                        "section_key": "",
                        "status": "failed",
                        "strategy": "",
                        "confidence": "",
                        "warnings": "",
                        "error": message,
                    }
                )
                cda_writer.writerow(
                    {
                        "cik": cik_padded,
                        "company_name": "",
                        "ticker": "",
                        "filing_date": "",
                        "fiscal_year": "",
                        "section_name": "",
                        "section_key": "",
                        "accession_number": "",
                        "filing_url": "",
                        "raw_html_path": "",
                        "start_anchor": "",
                        "end_anchor": "",
                        "start_page": "",
                        "end_page": "",
                        "strategy": "",
                        "warnings": "",
                        "confidence": "",
                        "markdown_path": "",
                        "markdown": "",
                        "status": "failed",
                        "error": message,
                    }
                )
                continue

            filing_records = records[: args.max_filings_per_cik]
            for filing_record in filing_records:
                try:
                    raw_html, resolved_record = _load_raw_html(
                        filing_record,
                        allow_fetch_fallback=not args.no_fetch_fallback,
                    )
                    doc_meta = _build_doc_metadata(resolved_record)
                    for section_name in SECTION_NAMES:
                        result = extract_section_markdown(
                            raw_html=raw_html,
                            metadata=doc_meta,
                            section_name=section_name,
                        )
                        section_key = (
                            result.section_key
                            or f"{resolved_record.cik}-{resolved_record.fiscal_year}-{section_name}"
                        )
                        status = "ok" if result.section_found else "missing"

                        markdown_path = ""
                        markdown_value = result.markdown if status == "ok" else ""
                        if not args.no_markdown_files and status == "ok":
                            markdown_filename = f"{section_key}.md"
                            file_path = markdown_dir / markdown_filename
                            file_path.write_text(result.markdown, encoding="utf-8")
                            markdown_path = str(file_path)

                        warnings_text = " | ".join(result.warnings)
                        cda_writer.writerow(
                            {
                                "cik": resolved_record.cik,
                                "company_name": resolved_record.company_name,
                                "ticker": resolved_record.ticker,
                                "filing_date": resolved_record.filing_date.isoformat(),
                                "fiscal_year": str(resolved_record.fiscal_year),
                                "section_name": section_name,
                                "section_key": section_key,
                                "accession_number": resolved_record.accession_number,
                                "filing_url": resolved_record.filing_url,
                                "raw_html_path": str(resolved_record.raw_html_path),
                                "start_anchor": result.start_anchor or "",
                                "end_anchor": result.end_anchor or "",
                                "start_page": result.start_page if result.start_page is not None else "",
                                "end_page": result.end_page if result.end_page is not None else "",
                                "strategy": result.strategy,
                                "warnings": warnings_text,
                                "confidence": result.confidence,
                                "markdown_path": markdown_path,
                                "markdown": markdown_value,
                                "status": status,
                                "error": "",
                            }
                        )

                        run_log_writer.writerow(
                            {
                                "cik": resolved_record.cik,
                                "company_name": resolved_record.company_name,
                                "accession_number": resolved_record.accession_number,
                                "filing_date": resolved_record.filing_date.isoformat(),
                                "fiscal_year": str(resolved_record.fiscal_year),
                                "section_name": section_name,
                                "section_key": section_key,
                                "status": status,
                                "strategy": result.strategy,
                                "confidence": result.confidence,
                                "warnings": warnings_text,
                                "error": "",
                            }
                        )
                        if status == "ok":
                            success_count += 1
                except Exception as exc:  # noqa: BLE001
                    error_message = str(exc)
                    for section_name in SECTION_NAMES:
                        section_key = f"{filing_record.cik}-{filing_record.fiscal_year}-{section_name}"
                        run_log_writer.writerow(
                            {
                                "cik": filing_record.cik,
                                "company_name": filing_record.company_name,
                                "accession_number": filing_record.accession_number,
                                "filing_date": filing_record.filing_date.isoformat(),
                                "fiscal_year": str(filing_record.fiscal_year),
                                "section_name": section_name,
                                "section_key": section_key,
                                "status": "failed",
                                "strategy": "",
                                "confidence": "",
                                "warnings": "",
                                "error": error_message,
                            }
                        )
                        cda_writer.writerow(
                            {
                                "cik": filing_record.cik,
                                "company_name": filing_record.company_name,
                                "ticker": filing_record.ticker,
                                "filing_date": filing_record.filing_date.isoformat(),
                                "fiscal_year": str(filing_record.fiscal_year),
                                "section_name": section_name,
                                "section_key": section_key,
                                "accession_number": filing_record.accession_number,
                                "filing_url": filing_record.filing_url,
                                "raw_html_path": str(filing_record.raw_html_path),
                                "start_anchor": "",
                                "end_anchor": "",
                                "start_page": "",
                                "end_page": "",
                                "strategy": "",
                                "warnings": "",
                                "confidence": "",
                                "markdown_path": "",
                                "markdown": "",
                                "status": "failed",
                                "error": error_message,
                            }
                        )
                        failure_count += 1

    log.info(
        "batch complete | success=%d failed=%d output=%s",
        success_count,
        failure_count,
        out_dir,
    )


if __name__ == "__main__":
    main()
