"""
Batch pipeline: CIK list -> one-row-per-company key_results CSV.

Usage
-----
    poetry run python scripts/run_batch50_key_results.py \
        --input fixtures/client_input.csv \
        --batch-label b01 \
        --limit 50 \
        --model gpt-4o-mini

What this script does
---------------------
For each CIK in the input CSV (up to --limit):

  1. ACQUIRE  fetch_latest_def14a(cik) -> FetchedFiling
              (HTML cached in data/raw/; CIK-only path, no folder_id)

  2. PARSE    SECHTMLParser().parse(raw_html, doc_meta) -> list[BaseBlock]
              (Task 2 fixes ensure compensation heading + XBRL numerics
               are correctly extracted)

  3. LOCATE   Find the Summary Compensation TableBlock:
              - Scan HeadingBlocks for text matching compensation signatures
              - Take the first TableBlock whose section_id == heading.id
              - Fallback: scan all TableBlocks within 15 blocks of any
                compensation-signature HeadingBlock

  4. EXTRACT  Try deterministic extractor first:
              comp_table_extractor.extract_summary_compensation(blocks, meta)
              If zero rows returned -> trigger LLM fallback:
              llm_comp_extractor.extract_company_comp_from_summary_table(...)

  5. COLLAPSE Deterministic rows -> collapse to one company record:
              For each role (CEO/CFO/COO), find matching row by title
              keywords, pick most recent year. Up to 2 others.
              LLM result -> already role-keyed, use directly.

  6. WRITE    Append one row to output/<batch_label>/key_results.csv
              Also append one row to output/<batch_label>/batch_log.csv

  7. END      Print summary. Exit non-zero if CEO total populated
              for fewer than MIN_CEO_COVERAGE companies.

Output columns - key_results.csv
---------------------------------
  cik, company_name, ticker, filing_date, fiscal_year,
  accession_number, filing_url,
  ceo_name, ceo_title, ceo_salary, ceo_bonus,
  ceo_stock_awards, ceo_option_awards, ceo_total,
  cfo_name, cfo_title, cfo_salary, cfo_total,
  coo_name, coo_title, coo_salary, coo_total,
  other1_name, other1_title, other1_salary, other1_total,
  other2_name, other2_title, other2_salary, other2_total,
  source_table_block_id, source_section_id,
  extraction_method,   (values: "deterministic" | "llm" | "failed")
  llm_model,           (model name if llm used, else "")
  llm_confidence,      (float if llm used, else "")
  status,              ("ok" | "failed")
  error                (empty string or error message)

Output columns - batch_log.csv
-------------------------------
  cik, company_name, status, extraction_method,
  block_count, table_count, comp_heading_found,
  comp_table_found, det_rows, llm_confidence,
  elapsed_seconds, error

Constants (configurable via CLI args)
--------------------------------------
  DEFAULT_LIMIT = 50
  DEFAULT_MODEL = "gpt-4o-mini"
  MIN_CEO_COVERAGE = 30   # exit non-zero if below this

Role keyword matching (for deterministic row collapse)
------------------------------------------------------
  CEO_KEYWORDS = {"chief executive officer", "ceo"}
  CFO_KEYWORDS = {"chief financial officer", "cfo"}
  COO_KEYWORDS = {"chief operating officer", "coo"}
  PRESIDENT_KEYWORDS = {"president"}   # fallback to ceo if no ceo found

The function _collapse_to_roles(det_rows) must:
  - For each role, filter rows where exec_name title field matches
    any keyword (case-insensitive)
  - Among matches, pick the row with the most recent year (or first
    if years are equal/missing)
  - If CEO not found but President found, assign President row -> CEO
  - Remaining rows (not CEO/CFO/COO, sorted by total desc) -> other1, other2

Error handling
--------------
  All per-CIK errors are caught and logged. A failed row is always
  written to both CSVs so the batch log is complete. The script
  never crashes on a single CIK failure.

DB writes (optional, non-blocking)
-----------------------------------
  If DB_URL is set in environment AND --no-db flag is NOT passed:
    - Chunks from SECChunker are written to Postgres via ChunkWriter
    - If DB write fails, log a warning and continue (do not fail CIK)
  If DB_URL is not set: skip silently.
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any, cast

import ingestion.cda_extractor as cda_extractor
import ingestion.comp_table_extractor as det_extractor
import ingestion.edgar_folder_fetcher as fetcher
from ingestion.llm_comp_extractor import ExecCompRecord, extract_company_comp_from_summary_table
from ingestion.metadata_model import BaseBlock, DocumentMetadata, HeadingBlock, TableBlock
from ingestion.sec_chunker import SECChunker
from ingestion.sec_html_parser import SECHTMLParser

log = logging.getLogger(__name__)

DEFAULT_LIMIT = 50
DEFAULT_MODEL = "gpt-4o-mini"
MIN_CEO_COVERAGE = 30
BATCH_OUTPUT_BASE = Path("output")

CEO_KEYWORDS = {"chief executive officer", "ceo"}
CFO_KEYWORDS = {"chief financial officer", "cfo"}
COO_KEYWORDS = {"chief operating officer", "coo"}
PRESIDENT_KEYWORDS = {"president"}

_COMP_SIGNATURES = [
    "summary compensation table",
    "summary compensation",
    "named executive officer compensation",
    "compensation of named executive officers",
]

KEY_RESULTS_COLUMNS = [
    "cik",
    "company_name",
    "ticker",
    "filing_date",
    "fiscal_year",
    "accession_number",
    "filing_url",
    "ceo_name",
    "ceo_title",
    "ceo_salary",
    "ceo_bonus",
    "ceo_stock_awards",
    "ceo_option_awards",
    "ceo_total",
    "cfo_name",
    "cfo_title",
    "cfo_salary",
    "cfo_total",
    "coo_name",
    "coo_title",
    "coo_salary",
    "coo_total",
    "other1_name",
    "other1_title",
    "other1_salary",
    "other1_total",
    "other2_name",
    "other2_title",
    "other2_salary",
    "other2_total",
    "source_table_block_id",
    "source_section_id",
    "extraction_method",
    "llm_model",
    "llm_confidence",
    "cda_token_count",
    "pay_for_performance_flag",
    "cda_section_found",
    "status",
    "error",
]

BATCH_LOG_COLUMNS = [
    "cik",
    "company_name",
    "status",
    "extraction_method",
    "block_count",
    "table_count",
    "comp_heading_found",
    "comp_table_found",
    "det_rows",
    "llm_confidence",
    "cda_token_count",
    "pay_for_performance_flag",
    "elapsed_seconds",
    "error",
]


def _fetch_latest_def14a(cik: str) -> fetcher.FetchedFiling:
    """Use fetch_latest_def14a when available; fallback to current fetch API."""
    fetch_latest = getattr(fetcher, "fetch_latest_def14a", None)
    if callable(fetch_latest):
        return cast(fetcher.FetchedFiling, fetch_latest(cik))
    return fetcher.fetch_filing(cik=cik, folder_id=cik, form_type="DEF 14A")


def _role_match(title: str, keywords: set[str]) -> bool:
    """Case-insensitive check if title contains any keyword."""
    lowered = title.lower()
    return any(keyword in lowered for keyword in keywords)


def _most_recent_row(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the row with the most recent fiscal year, or first row."""
    if not rows:
        return None

    def _year_key(row: dict[str, Any]) -> str:
        return str(row.get("year", "") or "")

    return sorted(rows, key=_year_key, reverse=True)[0]


def _to_float(value: Any) -> float:
    """Best-effort conversion to float for ranking totals."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _collapse_to_roles(det_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """
    Collapse deterministic extractor rows (one per exec per year)
    into a role-keyed dict: ceo, cfo, coo, other1, other2.

    Role assignment:
    - Match by title keywords (case-insensitive)
    - Among matches for a role, pick most recent year
    - President -> ceo if no ceo found
    - Remaining (not assigned, sorted by total desc) -> other1, other2
    """
    ceo_rows: list[dict[str, Any]] = []
    cfo_rows: list[dict[str, Any]] = []
    coo_rows: list[dict[str, Any]] = []
    president_rows: list[dict[str, Any]] = []
    other_rows: list[dict[str, Any]] = []
    assigned_names: set[str] = set()

    for row in det_rows:
        title_field = str(row.get("exec_title", "") or "")
        name_field = str(row.get("exec_name", "") or "")
        role_text = (title_field or name_field).lower()

        if _role_match(role_text, CEO_KEYWORDS):
            ceo_rows.append(row)
        elif _role_match(role_text, CFO_KEYWORDS):
            cfo_rows.append(row)
        elif _role_match(role_text, COO_KEYWORDS):
            coo_rows.append(row)
        elif _role_match(role_text, PRESIDENT_KEYWORDS):
            president_rows.append(row)
        else:
            other_rows.append(row)

    ceo = _most_recent_row(ceo_rows)
    if ceo is None:
        ceo = _most_recent_row(president_rows)
    cfo = _most_recent_row(cfo_rows)
    coo = _most_recent_row(coo_rows)

    for assigned_row in (ceo, cfo, coo):
        if assigned_row:
            assigned_names.add(str(assigned_row.get("exec_name", "") or ""))

    remaining = sorted(
        [row for row in other_rows if str(row.get("exec_name", "") or "") not in assigned_names],
        key=lambda row: _to_float(row.get("total")),
        reverse=True,
    )

    return {
        "ceo": ceo or {},
        "cfo": cfo or {},
        "coo": coo or {},
        "other1": remaining[0] if len(remaining) > 0 else {},
        "other2": remaining[1] if len(remaining) > 1 else {},
    }


def _locate_comp_table(blocks: list[BaseBlock]) -> tuple[TableBlock | None, HeadingBlock | None]:
    """
    Locate the Summary Compensation TableBlock and its parent heading.

    Strategy:
    1. Find all HeadingBlocks whose text matches compensation signatures.
    2. For each, find the first TableBlock with matching section_id.
    3. Fallback: scan all TableBlocks within 15 blocks of any matching
       heading (catches cases where section_id update was missed).

    Returns (TableBlock, HeadingBlock) or (None, None).
    """
    comp_headings = [
        block
        for block in blocks
        if isinstance(block, HeadingBlock)
        and any(signature in block.text.lower() for signature in _COMP_SIGNATURES)
    ]

    for heading in comp_headings:
        for block in blocks:
            if isinstance(block, TableBlock) and block.section_id == heading.id:
                return block, heading

    for heading_index, block in enumerate(blocks):
        if not isinstance(block, HeadingBlock):
            continue
        if not any(signature in block.text.lower() for signature in _COMP_SIGNATURES):
            continue
        for offset in range(1, 16):
            candidate_index = heading_index + offset
            if candidate_index >= len(blocks):
                break
            candidate = blocks[candidate_index]
            if isinstance(candidate, TableBlock):
                return candidate, block

    return None, None


def _row_from_det(role_dict: dict[str, Any], prefix: str) -> dict[str, Any]:
    """Build flat key_results columns for one role from a deterministic row."""
    base = {
        f"{prefix}_name": str(role_dict.get("exec_name", "") or ""),
        f"{prefix}_title": str(role_dict.get("exec_title", "") or ""),
        f"{prefix}_salary": role_dict.get("salary", ""),
        f"{prefix}_total": role_dict.get("total", ""),
    }
    if prefix == "ceo":
        base.update(
            {
                "ceo_bonus": role_dict.get("bonus", ""),
                "ceo_stock_awards": role_dict.get("stock_awards", ""),
                "ceo_option_awards": role_dict.get("option_awards", ""),
            }
        )
    return base


def _row_from_llm(record: ExecCompRecord, prefix: str) -> dict[str, Any]:
    """Build flat key_results columns for one role from an LLM ExecCompRecord."""
    base = {
        f"{prefix}_name": record.name,
        f"{prefix}_title": record.title,
        f"{prefix}_salary": record.salary or "",
        f"{prefix}_total": record.total or "",
    }
    if prefix == "ceo":
        base.update(
            {
                "ceo_bonus": record.bonus or "",
                "ceo_stock_awards": record.stock_awards or "",
                "ceo_option_awards": record.option_awards or "",
            }
        )
    return base


def _role_fiscal_year(
    extraction_method: str,
    roles: dict[str, Any],
) -> str:
    """Choose a representative fiscal year from assigned role rows."""
    for key in ("ceo", "cfo", "coo", "other1", "other2"):
        role = roles.get(key)
        if extraction_method == "llm" and isinstance(role, ExecCompRecord):
            if role.fiscal_year:
                return role.fiscal_year
        elif isinstance(role, dict):
            year = str(role.get("year", "") or "")
            if year:
                return year
    return ""


def process_cik(cik: str, model: str, skip_db: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Full pipeline for one CIK. Returns (key_results_row, log_row).

    This function never raises. All errors are caught and returned
    as a failed row so the batch loop stays clean.

    Pipeline stages:
      acquire -> parse -> locate -> extract (det or llm) -> collapse -> build rows
    """
    t0 = time.monotonic()

    def _failed(
        reason: str,
        company_name: str = "",
        extra: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        elapsed = round(time.monotonic() - t0, 2)
        base: dict[str, Any] = {col: "" for col in KEY_RESULTS_COLUMNS}
        base.update(
            {
                "cik": cik,
                "company_name": company_name,
                "extraction_method": "failed",
                "cda_token_count": 0,
                "pay_for_performance_flag": False,
                "cda_section_found": False,
                "status": "failed",
                "error": reason,
            }
        )
        log_row: dict[str, Any] = {
            "cik": cik,
            "company_name": company_name,
            "status": "failed",
            "extraction_method": "failed",
            "block_count": 0,
            "table_count": 0,
            "comp_heading_found": False,
            "comp_table_found": False,
            "det_rows": 0,
            "llm_confidence": "",
            "cda_token_count": 0,
            "pay_for_performance_flag": False,
            "elapsed_seconds": elapsed,
            "error": reason,
        }
        if extra:
            base.update(extra)
            for key, value in extra.items():
                if key in BATCH_LOG_COLUMNS:
                    log_row[key] = value
        return base, log_row

    try:
        filing = _fetch_latest_def14a(cik)
    except Exception as exc:  # noqa: BLE001
        return _failed(f"acquisition_failed: {exc}")

    company_name = str(getattr(filing, "company_name", "") or "")
    ticker = str(getattr(filing, "ticker", "") or "")

    try:
        doc_meta = DocumentMetadata(
            document_id=f"{cik}_{filing.accession_number.replace('-', '')}",
            cik=cik,
            company_name=company_name,
            form_type="DEF 14A",
            filing_date=filing.filing_date or date.today(),
            accession_number=filing.accession_number,
            source_url=filing.filing_url,
        )
        blocks = SECHTMLParser().parse(filing.raw_html, doc_meta)
    except Exception as exc:  # noqa: BLE001
        return _failed(f"parse_failed: {exc}", company_name)

    meta_dict: dict[str, Any] = {
        "cik": cik,
        "company_name": company_name,
        "filing_date": str(filing.filing_date or ""),
        "accession_number": filing.accession_number,
    }

    try:
        cda_row = cda_extractor.extract_cda(blocks, meta_dict)
        cda_token_count = cda_row.get("cda_token_count", 0)
        pay_for_performance_flag = cda_row.get("pay_for_performance_flag", False)
        cda_section_found = cda_row.get("cda_section_found", False)
    except Exception as cda_exc:  # noqa: BLE001
        log.warning("cda_extraction_failed | cik=%s error=%s", cik, cda_exc)
        cda_token_count = 0
        pay_for_performance_flag = False
        cda_section_found = False

    table_blocks = [block for block in blocks if isinstance(block, TableBlock)]
    block_count = len(blocks)
    table_count = len(table_blocks)

    comp_table, comp_heading = _locate_comp_table(blocks)
    comp_heading_found = comp_heading is not None
    comp_table_found = comp_table is not None

    extraction_method = "failed"
    llm_confidence: float | str = ""
    llm_model_used = ""
    source_table_block_id = comp_table.id if comp_table else ""
    source_section_id = comp_table.section_id if comp_table else ""
    roles: dict[str, Any] = {}

    try:
        det_rows = det_extractor.extract_summary_compensation(blocks, meta_dict)
    except Exception as exc:  # noqa: BLE001
        return _failed(
            f"deterministic_extract_failed: {exc}",
            company_name,
            {
                "block_count": block_count,
                "table_count": table_count,
                "comp_heading_found": comp_heading_found,
                "comp_table_found": comp_table_found,
            },
        )

    if det_rows:
        extraction_method = "deterministic"
        roles = _collapse_to_roles(det_rows)
    elif comp_table is not None:
        log.info("llm_fallback triggered | cik=%s", cik)
        llm_result = extract_company_comp_from_summary_table(
            company_name=company_name,
            cik=cik,
            filing_date=str(filing.filing_date or ""),
            accession_number=filing.accession_number,
            table_text=comp_table.linearized_text,
            model=model,
        )
        llm_confidence = llm_result.confidence
        llm_model_used = model
        extraction_method = "llm"
        roles = {
            "ceo": llm_result.ceo,
            "cfo": llm_result.cfo,
            "coo": llm_result.coo,
            "other1": llm_result.other1,
            "other2": llm_result.other2,
        }
    else:
        return _failed(
            "no_comp_table_located",
            company_name,
            {
                "block_count": block_count,
                "table_count": table_count,
                "comp_heading_found": comp_heading_found,
                "comp_table_found": comp_table_found,
            },
        )

    fiscal_year_val = ""
    for role_key in ["ceo", "cfo", "coo", "other1", "other2"]:
        candidate = roles.get(role_key, {})
        year_val = (
            candidate.get("year", "")
            if isinstance(candidate, dict)
            else getattr(candidate, "fiscal_year", "")
        )
        if year_val:
            fiscal_year_val = str(year_val)
            break

    result_row: dict[str, Any] = {col: "" for col in KEY_RESULTS_COLUMNS}
    result_row.update(
        {
            "cik": cik,
            "company_name": company_name,
            "ticker": ticker,
            "filing_date": str(filing.filing_date or ""),
            "fiscal_year": fiscal_year_val,
            "accession_number": filing.accession_number,
            "filing_url": filing.filing_url,
            "source_table_block_id": source_table_block_id,
            "source_section_id": source_section_id,
            "extraction_method": extraction_method,
            "llm_model": llm_model_used,
            "llm_confidence": llm_confidence,
            "cda_token_count": cda_token_count,
            "pay_for_performance_flag": pay_for_performance_flag,
            "cda_section_found": cda_section_found,
            "status": "ok",
            "error": "",
        }
    )

    for prefix in ("ceo", "cfo", "coo", "other1", "other2"):
        role_data = roles.get(prefix)
        if extraction_method == "llm" and isinstance(role_data, ExecCompRecord):
            result_row.update(_row_from_llm(role_data, prefix))
            continue
        if isinstance(role_data, dict):
            result_row.update(_row_from_det(role_data, prefix))

    elapsed = round(time.monotonic() - t0, 2)
    log_row: dict[str, Any] = {
        "cik": cik,
        "company_name": company_name,
        "status": "ok",
        "extraction_method": extraction_method,
        "block_count": block_count,
        "table_count": table_count,
        "comp_heading_found": comp_heading_found,
        "comp_table_found": comp_table_found,
        "det_rows": len(det_rows),
        "llm_confidence": llm_confidence,
        "cda_token_count": cda_token_count,
        "pay_for_performance_flag": pay_for_performance_flag,
        "elapsed_seconds": elapsed,
        "error": "",
    }

    if not skip_db and os.environ.get("DB_URL"):
        try:
            from storage.writer import ChunkWriter

            chunks = SECChunker().chunk_blocks(blocks, doc_meta)
            ChunkWriter().write_chunks(chunks, doc_meta)
            log.info("db_write | cik=%s chunks=%d", cik, len(chunks))
        except Exception as db_exc:  # noqa: BLE001
            log.warning("db_write_failed | cik=%s error=%s", cik, db_exc)

    return result_row, log_row


def main() -> None:
    """CLI entrypoint for batch key results generation."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    )

    parser = argparse.ArgumentParser(description="Batch pipeline: CIK list -> key_results.csv")
    parser.add_argument(
        "--input",
        default="fixtures/client_input.csv",
        help="CSV file with 'cik' column (default: fixtures/client_input.csv)",
    )
    parser.add_argument(
        "--batch-label",
        default="b01",
        help="Output subfolder label (default: b01)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=DEFAULT_LIMIT,
        help=f"Max CIKs to process (default: {DEFAULT_LIMIT})",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"OpenAI model for LLM fallback (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--no-db",
        action="store_true",
        help="Skip Postgres chunk writes even if DB_URL is set",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        log.error("Input file not found: %s", input_path)
        sys.exit(1)

    with input_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        cik_rows = [row for row in reader]
        fieldnames = reader.fieldnames or []

    cik_col = next(
        (column for column in fieldnames if column.lower() in {"cik", "folder_id"}),
        fieldnames[0] if fieldnames else "cik",
    )
    ciks = [row[cik_col].strip() for row in cik_rows if row.get(cik_col, "").strip()]
    ciks = ciks[: args.limit]

    log.info("batch start | label=%s ciks=%d model=%s", args.batch_label, len(ciks), args.model)

    out_dir = BATCH_OUTPUT_BASE / args.batch_label
    out_dir.mkdir(parents=True, exist_ok=True)
    key_results_path = out_dir / "key_results.csv"
    batch_log_path = out_dir / "batch_log.csv"

    success_count = 0
    failed_count = 0
    ceo_total_populated = 0

    with (
        key_results_path.open("w", newline="", encoding="utf-8") as key_results_file,
        batch_log_path.open("w", newline="", encoding="utf-8") as batch_log_file,
    ):
        kr_writer = csv.DictWriter(key_results_file, fieldnames=KEY_RESULTS_COLUMNS)
        log_writer = csv.DictWriter(batch_log_file, fieldnames=BATCH_LOG_COLUMNS)
        kr_writer.writeheader()
        log_writer.writeheader()

        for index, cik in enumerate(ciks, start=1):
            log.info("[%d/%d] processing | cik=%s", index, len(ciks), cik)
            result_row, log_row = process_cik(cik, args.model, args.no_db)
            kr_writer.writerow(result_row)
            log_writer.writerow(log_row)
            key_results_file.flush()
            batch_log_file.flush()

            if result_row.get("status") == "ok":
                success_count += 1
                if result_row.get("ceo_total"):
                    ceo_total_populated += 1
            else:
                failed_count += 1

    log.info(
        "batch complete | success=%d failed=%d ceo_total_populated=%d/%d",
        success_count,
        failed_count,
        ceo_total_populated,
        len(ciks),
    )
    log.info("key_results -> %s", key_results_path)
    log.info("batch_log   -> %s", batch_log_path)

    if ceo_total_populated < MIN_CEO_COVERAGE:
        log.error(
            "COVERAGE BELOW THRESHOLD: CEO total populated for %d/%d companies "
            "(minimum required: %d). Review batch_log.csv for failure reasons.",
            ceo_total_populated,
            len(ciks),
            MIN_CEO_COVERAGE,
        )
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
