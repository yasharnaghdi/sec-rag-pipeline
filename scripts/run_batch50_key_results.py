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
  cik, company_name, run_scope, target_fiscal_year,
  source_filing_date, source_accession_number, source_filing_url,
  status, extraction_method,
  block_count, table_count, comp_heading_found,
  comp_table_found, grant_table_found, det_rows, llm_confidence,
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
import inspect
import logging
import os
import re
import sys
import time
from datetime import date
from pathlib import Path
from typing import Any, cast

from dotenv import load_dotenv

if __package__ in {None, ""}:
    project_root = Path(__file__).resolve().parents[1]
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
else:
    project_root = Path(__file__).resolve().parents[1]

# Ensure local .env is loaded when running this script directly.
load_dotenv(project_root / ".env", override=False)

import ingestion.cda_extractor as cda_extractor  # noqa: E402
import ingestion.comp_table_extractor as det_extractor  # noqa: E402
import ingestion.edgar_folder_fetcher as fetcher  # noqa: E402
from ingestion.llm_comp_extractor import (  # noqa: E402
    CompanyGrantsResult,
    CompanyOutstandingEquityAwardsResult,
    ExecCompRecord,
    GrantPlanAwardRecord,
    OutstandingEquityAwardRecord,
    extract_company_comp_from_summary_table,
    extract_grants_from_plan_based_table,
    extract_outstanding_equity_awards_table,
)
from ingestion.metadata_model import BaseBlock, DocumentMetadata, HeadingBlock, ProseBlock, TableBlock  # noqa: E402
from ingestion.sec_chunker import SECChunker  # noqa: E402
from ingestion.sec_html_parser import SECHTMLParser  # noqa: E402

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
_GRANTS_SIGNATURES = [
    "grants of plan-based awards",
    "grants of plan based awards",
    "grant of plan-based award",
    "grant of plan based award",
    "incentive plan awards",
    "plan-based award grants",
]
_COMP_NAME_HEADER_HINTS = {
    "name and principal position",
    "name",
    "principal position",
    "executive officer",
    "named executive",
}
_COMP_VALUE_HEADER_HINTS = {
    "salary",
    "bonus",
    "stock awards",
    "option awards",
    "non-equity",
    "non equity",
    "all other compensation",
    "total",
    "compensation",
    "fiscal year",
    "year",
}
_COMP_REJECT_HEADER_HINTS = {
    "beneficial ownership",
    "principal stockholder",
    "principal stockholders",
    "shares owned",
    "percent of shares",
    "percentage",
    "peer group",
    "stockholder return",
    "total stockholder return",
}

_GRANTS_REQUIRED_SCORE_TERMS = {
    "grant",
    "incentive plan award",
}
_GRANTS_HEADER_HINTS = {
    "grant date",
    "award type",
    "threshold",
    "target",
    "maximum",
    "all other stock awards",
    "all other option awards",
    "exercise or base price",
    "grant date fair value",
    "non-equity incentive plan awards",
    "non equity incentive plan awards",
    "equity incentive plan awards",
}
_GRANTS_GRANT_TYPE_HINTS = {
    "annual incentive award",
    "aia",
    "performance restricted stock unit",
    "prsu",
    "performance-based rsu",
    "performance based rsu",
    "time-lapse rsu",
    "time lapse rsu",
    "stock option",
    "incentive plan",
}

_OUTSTANDING_EQUITY_SIGNATURES = [
    "outstanding equity awards at fiscal year-end",
    "outstanding equity awards at fiscal year end",
    "outstanding equity awards at year-end",
    "outstanding equity awards",
    "equity awards outstanding",
]
_OUTSTANDING_EQUITY_REQUIRED_TERMS = {
    "outstanding",
    "equity",
    "awards",
}
_OUTSTANDING_EQUITY_HEADER_HINTS = {
    "grant date",
    "unexercised options",
    "exercisable",
    "unexercisable",
    "unearned options",
    "option exercise price",
    "option expiration date",
    "have not vested",
    "market value",
    "payout value",
    "stock awards",
    "option awards",
    "equity incentive plan awards",
}

GRANTS_OUTPUT_COLUMNS = [
    "CIK",
    "Company Name",
    "Filing URL",
    "Name",
    "Grant Type",
    "Grant Date",
    "Estimated future payouts under non-equity incentive plan awards (Threshold)",
    "Estimated future payouts under non-equity incentive plan awards (Target)",
    "Estimated future payouts under non-equity incentive plan awards (Maximum)",
    "Estimated future payouts under equity incentive plan awards (Threshold)",
    "Estimated future payouts under equity incentive plan awards (Target)",
    "Estimated future payouts under equity incentive plan awards (Maximum)",
    "All other stock awards: Number of shares of stock or units",
    "All other option awards: Number of securities underlying options",
    "Exercise or base price of option awards",
    "Grant date fair value of stock and option awards",
]

OUTSTANDING_EQUITY_AWARDS_OUTPUT_COLUMNS = [
    "CIK",
    "Company Name",
    "Filing URL",
    "Name",
    "Grant Date",
    "Number of Securities Underlying Unexercised Options Exercisable (#)",
    "Number of Securities Underlying Unexercised Options Unexercisable (#)",
    "Equity Incentive Plan Awards: Number of Securities Underlying Unexercised Unearned Options (#)",
    "Option Exercise Price ($)",
    "Option Expiration Date",
    "Number of Shares or Units of Stock that Have Not Vested (#)",
    "Market Value of Shares or Units of Stock that Have Not Vested ($)",
    "Equity Incentive Plan Awards: Number of Unearned Shares, Units, or Other Rights that Have Not Vested (#)",
    "Equity Incentive Plan Awards: Market or Payout Value of Unearned Shares, Units, or Other Rights that Have Not Vested ($)",
]

COMPENSATION_OUTPUT_COLUMNS = [
    "CIK",
    "Company Name",
    "Filing URL",
    "ticker",
    "Name",
    "Title",
    "Year",
    "Salary ($)",
    "Bonus Awards ($)",
    "Stock Awards ($)",
    "Option Awards ($)",
    "Non-Equity Incentive Plan Compensation ($)",
    "Change in pension value and nonqualified deferred compensation earnings ($)",
    "All Other Compensation ($)",
    "Total ($)",
    "Extra information",
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
    "run_scope",
    "target_fiscal_year",
    "source_filing_date",
    "source_accession_number",
    "source_filing_url",
    "status",
    "extraction_method",
    "block_count",
    "table_count",
    "comp_heading_found",
    "comp_table_found",
    "grant_table_found",
    "det_rows",
    "llm_confidence",
    "cda_token_count",
    "pay_for_performance_flag",
    "elapsed_seconds",
    "error",
]


def _call_process_cik(
    cik: str,
    model: str,
    skip_db: bool,
    fiscal_year_start: int | None = None,
    fiscal_year_end: int | None = None,
    filing_override: fetcher.FetchedFiling | None = None,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], list[dict[str, str]], list[dict[str, Any]]]:
    """
    Call process_cik with backward-compatible argument handling.

    Some tests monkeypatch process_cik with legacy signatures.
    """
    process_fn = cast(Any, process_cik)
    has_filing_override = False
    param_count = 0
    try:
        params = inspect.signature(process_fn).parameters
        has_filing_override = "filing_override" in params
        param_count = len(params)
    except (TypeError, ValueError):
        pass

    if has_filing_override or param_count >= 6:
        return cast(
            tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], list[dict[str, str]], list[dict[str, Any]]],
            process_fn(cik, model, skip_db, fiscal_year_start, fiscal_year_end, filing_override),
        )
    if param_count >= 5:
        return cast(
            tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], list[dict[str, str]], list[dict[str, Any]]],
            process_fn(cik, model, skip_db, fiscal_year_start, fiscal_year_end),
        )
    return cast(
        tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], list[dict[str, str]], list[dict[str, Any]]],
        process_fn(cik, model, skip_db),
    )


def _extract_summary_compensation_rows(
    blocks: list[BaseBlock],
    meta: dict[str, Any],
    selected_table: TableBlock | None,
) -> list[dict[str, Any]]:
    """Call deterministic summary comp extractor with signature compatibility."""
    extract_fn = cast(Any, det_extractor.extract_summary_compensation)
    has_selected_table_param = False
    try:
        has_selected_table_param = "selected_table" in inspect.signature(extract_fn).parameters
    except (TypeError, ValueError):
        has_selected_table_param = False

    if selected_table is not None and has_selected_table_param:
        return cast(
            list[dict[str, Any]],
            extract_fn(blocks, meta, selected_table=selected_table),
        )
    return cast(list[dict[str, Any]], extract_fn(blocks, meta))


def _enrich_log_row(
    raw_log_row: dict[str, Any],
    *,
    run_scope: str,
    target_fiscal_year: int | None = None,
    filing: fetcher.FetchedFiling | None = None,
    override_status: str | None = None,
    override_error: str | None = None,
    cik: str | None = None,
    company_name: str | None = None,
) -> dict[str, Any]:
    row: dict[str, Any] = {column: "" for column in BATCH_LOG_COLUMNS}
    for key, value in raw_log_row.items():
        if key in row:
            row[key] = value

    if cik is not None:
        row["cik"] = cik
    if company_name is not None:
        row["company_name"] = company_name
    row["run_scope"] = run_scope
    row["target_fiscal_year"] = str(target_fiscal_year) if target_fiscal_year is not None else ""

    if filing is not None:
        row["source_filing_date"] = str(filing.filing_date or "")
        row["source_accession_number"] = filing.accession_number
        row["source_filing_url"] = filing.filing_url

    if override_status is not None:
        row["status"] = override_status
    if override_error is not None:
        row["error"] = override_error
    if override_status == "failed" and not row.get("extraction_method"):
        row["extraction_method"] = "failed"

    return row


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


def _as_text(value: Any) -> str:
    return "" if value is None else str(value)


def _normalize_llm_comp_cell(cell: str) -> str:
    text = str(cell or "").replace("\xa0", " ")
    return " ".join(text.split()).strip()


def _is_standalone_footnote_marker(cell: str) -> bool:
    """True when a cell only contains one or more parenthetical footnote markers."""
    value = cell.strip()
    return bool(re.fullmatch(r"(?:\(\d+\))+", value))


def _extract_unique_year_tokens(cell: str) -> list[str]:
    tokens = re.findall(r"\b(?:19|20)\d{2}\b", cell)
    unique: list[str] = []
    for token in tokens:
        if token not in unique:
            unique.append(token)
    return unique


def _dedupe_adjacent_tokens(tokens: list[str]) -> list[str]:
    if not tokens:
        return []
    deduped = [tokens[0]]
    for token in tokens[1:]:
        if token != deduped[-1]:
            deduped.append(token)
    return deduped


def _split_multi_year_value_cell(cell: str, year_count: int) -> list[str]:
    """Split one fused value cell into per-year values when possible."""
    if year_count <= 1:
        return [cell]

    text = cell.strip()
    if not text:
        return ["" for _ in range(year_count)]

    lowered = text.lower()
    if lowered in {"-", "—", "–", "null", "none", "n/a", "na"}:
        return ["" for _ in range(year_count)]

    comma_tokens = re.findall(r"\d{1,3}(?:,\d{3})+(?:\.\d+)?(?:\s*\(\d+\))?", text)
    generic_tokens = re.findall(r"\d+(?:\.\d+)?(?:\s*\(\d+\))?", text)
    tokens = comma_tokens if comma_tokens else generic_tokens
    tokens = _dedupe_adjacent_tokens(tokens)

    if len(tokens) == year_count:
        return tokens
    if len(tokens) > year_count and len(tokens) % year_count == 0:
        block = len(tokens) // year_count
        return [tokens[index * block] for index in range(year_count)]
    if len(tokens) > year_count:
        return tokens[:year_count]
    if len(tokens) == 1:
        return [tokens[0]] + ["" for _ in range(year_count - 1)]
    return [text] + ["" for _ in range(year_count - 1)]


def _expand_multi_year_comp_row(
    cleaned_row: list[str],
    target_fiscal_year: int | None = None,
) -> list[list[str]]:
    """Expand rows that contain multiple fiscal years fused in one row."""
    year_col_index: int | None = None
    year_tokens: list[str] = []

    for index, cell in enumerate(cleaned_row):
        tokens = _extract_unique_year_tokens(cell)
        if len(tokens) > 1:
            year_col_index = index
            year_tokens = tokens
            break

    if year_col_index is None or len(year_tokens) <= 1:
        return [cleaned_row]

    year_count = len(year_tokens)
    expanded_columns: list[list[str]] = []
    for index, cell in enumerate(cleaned_row):
        if index < year_col_index:
            expanded_columns.append([cell for _ in range(year_count)])
            continue
        if index == year_col_index:
            expanded_columns.append(year_tokens)
            continue
        expanded_columns.append(_split_multi_year_value_cell(cell, year_count))

    selected_indexes = list(range(year_count))
    if target_fiscal_year is not None:
        target_year_text = str(target_fiscal_year)
        selected_indexes = [
            index for index, year_text in enumerate(year_tokens) if year_text == target_year_text
        ]
        if not selected_indexes:
            selected_indexes = list(range(year_count))

    rows_out: list[list[str]] = []
    for year_index in selected_indexes:
        split_row = [
            col_values[year_index] if year_index < len(col_values) else ""
            for col_values in expanded_columns
        ]
        rows_out.append(split_row)
    return rows_out


def _clean_llm_comp_row(raw_row: list[str]) -> list[str]:
    """Clean one Summary Compensation table row for LLM consumption."""
    normalized_cells = [_normalize_llm_comp_cell(cell) for cell in raw_row]
    merged: list[str] = []
    index = 0
    while index < len(normalized_cells):
        cell = normalized_cells[index]
        if not cell:
            index += 1
            continue
        if cell in {"$", "US$", "USD"}:
            next_index = index + 1
            if next_index < len(normalized_cells):
                next_cell = normalized_cells[next_index]
                if next_cell and any(char.isdigit() for char in next_cell):
                    merged.append(next_cell)
                    index += 2
                    continue
            index += 1
            continue
        if _is_standalone_footnote_marker(cell) and merged:
            merged[-1] = f"{merged[-1]} {cell}".strip()
            index += 1
            continue
        merged.append(cell)
        index += 1

    deduped: list[str] = []
    for cell in merged:
        if deduped and cell == deduped[-1]:
            continue
        deduped.append(cell)
    return deduped


def _build_llm_comp_table_text(
    table: TableBlock,
    target_fiscal_year: int | None = None,
) -> str:
    """Build a cleaner row-preserving table text for LLM extraction."""
    cleaned_lines: list[str] = []
    for row_index, raw_row in enumerate(table.rows, start=1):
        cleaned_row = _clean_llm_comp_row(raw_row)
        if not cleaned_row:
            continue
        expanded_rows = _expand_multi_year_comp_row(cleaned_row, target_fiscal_year)
        if len(expanded_rows) == 1:
            cleaned_lines.append(f"row {row_index}: {' | '.join(expanded_rows[0])}")
            continue
        for expanded_index, expanded_row in enumerate(expanded_rows, start=1):
            cleaned_lines.append(f"row {row_index}.{expanded_index}: {' | '.join(expanded_row)}")
    if cleaned_lines:
        return "\n".join(cleaned_lines)
    return table.linearized_text


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

    remaining_by_exec: dict[str, dict[str, Any]] = {}
    for row in other_rows:
        exec_name = str(row.get("exec_name", "") or "")
        if exec_name in assigned_names:
            continue
        prior = remaining_by_exec.get(exec_name)
        if prior is None:
            remaining_by_exec[exec_name] = row
            continue
        chosen = _most_recent_row([prior, row])
        if chosen is not None:
            remaining_by_exec[exec_name] = chosen

    remaining = sorted(
        remaining_by_exec.values(),
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
    def _heading_is_comp_signature(text: str) -> bool:
        normalized = " ".join(text.lower().split())
        if not any(signature in normalized for signature in _COMP_SIGNATURES):
            return False

        # Keep concise heading-like rows and drop long prose sentences
        # that merely mention "Summary Compensation Table".
        if len(normalized) <= 120:
            return True
        if any(normalized.startswith(signature) for signature in _COMP_SIGNATURES):
            return True
        return len(normalized.split()) <= 16

    def _table_prefix_text(table: TableBlock, max_rows: int = 12) -> str:
        if not table.rows:
            return ""
        parts: list[str] = []
        for row in table.rows[: min(max_rows, len(table.rows))]:
            parts.extend(cell.strip() for cell in row if cell.strip())
        return " | ".join(parts).lower()

    def _table_header_text(table: TableBlock) -> str:
        if not table.rows:
            return ""
        scan_rows = min(12, len(table.rows))
        hint_terms = _COMP_NAME_HEADER_HINTS | _COMP_VALUE_HEADER_HINTS | {"name"}
        scored_rows: list[tuple[int, int, list[str]]] = []

        for row_index in range(scan_rows):
            cells = [cell.strip() for cell in table.rows[row_index] if cell.strip()]
            if not cells:
                continue
            row_text = " | ".join(cells).lower()
            alpha_cells = sum(1 for cell in cells if any(char.isalpha() for char in cell))
            digit_cells = sum(1 for cell in cells if any(char.isdigit() for char in cell))
            score = (alpha_cells * 2) - digit_cells
            if len(cells) >= 4:
                score += 2
            if any(term in row_text for term in hint_terms):
                score += 10
            if "table of contents" in row_text:
                score -= 4
            if any(hint in row_text for hint in _COMP_REJECT_HEADER_HINTS):
                score -= 6
            scored_rows.append((score, row_index, cells))

        if not scored_rows:
            return ""

        scored_rows.sort(key=lambda item: (item[0], -item[1]), reverse=True)
        selected = sorted(scored_rows[:4], key=lambda item: item[1])
        parts: list[str] = []
        for _, _, cells in selected:
            parts.extend(cells)
        return " | ".join(parts).lower()

    def _comp_schema_fit(header_text: str, prefix_text: str) -> bool:
        combined = f"{header_text} | {prefix_text}"
        has_name_axis = (
            "name and principal position" in combined
            or ("name" in combined and "principal position" in combined)
            or ("name" in combined and "salary" in combined)
        )
        has_comp_columns = any(
            token in combined
            for token in (
                "salary",
                "bonus",
                "stock awards",
                "option awards",
                "non-equity incentive plan compensation",
                "non equity incentive plan compensation",
                "all other compensation",
            )
        )
        has_total = "total" in combined

        reject_tokens = set(_COMP_REJECT_HEADER_HINTS) | {
            "election of directors",
            "ratification of the appointment",
            "plan category",
            "beneficial owner",
            "beneficial owners",
            "shares purchased",
            "percent of common stock",
            "shareholder proposal",
        }
        if any(token in combined for token in reject_tokens):
            return False

        return has_name_axis and has_comp_columns and has_total

    def _score_comp_table_candidate(table: TableBlock, heading_text: str) -> int:
        header_text = _table_header_text(table)
        prefix_text = _table_prefix_text(table)
        if not header_text and not prefix_text:
            return -10
        if not _comp_schema_fit(header_text, prefix_text):
            return -12

        row_count = len(table.rows)
        col_count = max((len(row) for row in table.rows), default=0)
        header_rows = table.header_row_count if table.header_row_count > 0 else min(2, row_count)
        header_rows = min(max(1, header_rows), row_count) if row_count else 0
        data_row_count = max(0, row_count - header_rows)

        score = 0
        if any(hint in header_text for hint in _COMP_NAME_HEADER_HINTS):
            score += 3
        if any(hint in header_text for hint in _COMP_VALUE_HEADER_HINTS):
            score += 3
        if "salary" in header_text and "total" in header_text:
            score += 2

        # Summary compensation tables are typically matrix-style;
        # tiny 1-row/2-col note tables are usually footnotes.
        if row_count >= 4:
            score += 2
        elif row_count <= 2:
            score -= 8
        if col_count >= 5:
            score += 2
        elif col_count <= 3:
            score -= 4
        if data_row_count <= 1:
            score -= 4

        heading_lc = heading_text.lower()
        if any(signature in heading_lc for signature in _COMP_SIGNATURES):
            score += 2

        if any(hint in header_text for hint in _COMP_REJECT_HEADER_HINTS):
            score -= 6
        if "beneficial ownership" in header_text:
            score -= 10

        return score

    comp_headings: list[tuple[int, HeadingBlock]] = [
        (index, block)
        for index, block in enumerate(blocks)
        if isinstance(block, HeadingBlock)
        and _heading_is_comp_signature(block.text)
    ]

    scored_candidates: list[tuple[int, int, int, TableBlock, HeadingBlock | None]] = []

    for heading_index, heading in comp_headings:
        # Section-linked candidates.
        for block in blocks:
            if not isinstance(block, TableBlock):
                continue
            if block.section_id != heading.id:
                continue
            score = _score_comp_table_candidate(block, heading.text)
            if score >= 4:
                scored_candidates.append((score, 1, 0, block, heading))

        # Nearby fallback candidates.
        for offset in range(1, 16):
            candidate_index = heading_index + offset
            if candidate_index >= len(blocks):
                break
            candidate = blocks[candidate_index]
            if not isinstance(candidate, TableBlock):
                continue
            score = _score_comp_table_candidate(candidate, heading.text)
            if score >= 4:
                scored_candidates.append((score, 0, -offset, candidate, heading))

    if scored_candidates:
        best = max(scored_candidates, key=lambda item: (item[0], item[1], item[2]))
        return best[3], best[4]

    # Last fallback: choose a globally strong summary-comp style table,
    # even if heading linking failed.
    global_candidates: list[tuple[int, TableBlock]] = []
    for block in blocks:
        if not isinstance(block, TableBlock):
            continue
        score = _score_comp_table_candidate(block, "")
        header_text = _table_header_text(block)
        prefix_text = _table_prefix_text(block)
        if score >= 6 and _comp_schema_fit(header_text, prefix_text):
            global_candidates.append((score, block))
    if global_candidates:
        best_global = max(global_candidates, key=lambda item: item[0])
        return best_global[1], None

    return None, None


def _row_from_det(role_dict: dict[str, Any], prefix: str) -> dict[str, Any]:
    """Build flat key_results columns for one role from a deterministic row."""
    base = {
        f"{prefix}_name": str(role_dict.get("exec_name", "") or ""),
        f"{prefix}_title": str(role_dict.get("exec_title", "") or ""),
        f"{prefix}_salary": _as_digit_string(role_dict.get("salary")),
        f"{prefix}_total": _as_digit_string(role_dict.get("total")),
    }
    if prefix == "ceo":
        base.update(
            {
                "ceo_bonus": _as_digit_string(role_dict.get("bonus")),
                "ceo_stock_awards": _as_digit_string(role_dict.get("stock_awards")),
                "ceo_option_awards": _as_digit_string(role_dict.get("option_awards")),
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


def _det_rows_have_comp_payload(det_rows: list[dict[str, Any]]) -> bool:
    """Return True only for rows that look like real named-exec compensation."""
    empty_markers = {"", "-", "—", "$", "n/a", "na", "none"}

    def _has_numeric_amount(value: Any) -> bool:
        if isinstance(value, (int, float)):
            return True
        text = str(value or "").strip().lower()
        if not text or text in empty_markers:
            return False
        return any(char.isdigit() for char in text)

    has_year_signal = any(
        bool(re.fullmatch(r"\d{4}", str(row.get("year", "") or "").strip()))
        for row in det_rows
    )
    if not has_year_signal:
        return False

    for row in det_rows:
        exec_name = str(row.get("exec_name", "") or "").strip()
        if not exec_name:
            continue
        if _has_numeric_amount(row.get("salary")) or _has_numeric_amount(row.get("total")):
            return True
    return False


def _llm_result_has_comp_payload(result: Any) -> bool:
    """Return True if LLM result contains at least one populated role value."""
    llm_rows = getattr(result, "rows", None)
    if isinstance(llm_rows, list):
        for row in llm_rows:
            if not isinstance(row, ExecCompRecord):
                continue
            values = [
                str(row.name or "").strip(),
                str(row.title or "").strip(),
                str(row.salary or "").strip(),
                str(row.bonus or "").strip(),
                str(row.stock_awards or "").strip(),
                str(row.option_awards or "").strip(),
                str(row.non_equity_incentive or "").strip(),
                str(row.pension_change or "").strip(),
                str(row.other_comp or "").strip(),
                str(row.total or "").strip(),
                str(row.footnotes or "").strip(),
                str(row.fiscal_year or "").strip(),
            ]
            if any(values):
                return True

    roles = [
        getattr(result, "ceo", None),
        getattr(result, "cfo", None),
        getattr(result, "coo", None),
        getattr(result, "other1", None),
        getattr(result, "other2", None),
    ]
    for role in roles:
        if role is None:
            continue
        values = [
            str(getattr(role, "name", "") or "").strip(),
            str(getattr(role, "title", "") or "").strip(),
            str(getattr(role, "salary", "") or "").strip(),
            str(getattr(role, "bonus", "") or "").strip(),
            str(getattr(role, "stock_awards", "") or "").strip(),
            str(getattr(role, "option_awards", "") or "").strip(),
            str(getattr(role, "non_equity_incentive", "") or "").strip(),
            str(getattr(role, "pension_change", "") or "").strip(),
            str(getattr(role, "other_comp", "") or "").strip(),
            str(getattr(role, "total", "") or "").strip(),
            str(getattr(role, "footnotes", "") or "").strip(),
        ]
        if any(value for value in values):
            return True
    return False


def _result_row_has_comp_payload(result_row: dict[str, Any]) -> bool:
    """Return True if mapped output row has at least one numeric comp value."""
    comp_fields = (
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
    )
    empty_markers = {"", "-", "—", "$", "n/a", "na", "none"}

    for field in comp_fields:
        text = str(result_row.get(field, "") or "").strip().lower()
        if not text or text in empty_markers:
            continue
        if any(char.isdigit() for char in text):
            return True
    return False


def _infer_fiscal_year_from_filing_date(filing_date: date | None) -> str:
    """Infer fiscal year using Jan-Aug => prior year, Sep-Dec => current year."""
    if filing_date is None:
        return ""
    return str(filing_date.year - 1 if filing_date.month <= 8 else filing_date.year)


def _parse_fiscal_year(value: str) -> int | None:
    text = value.strip()
    if not re.fullmatch(r"\d{4}", text):
        return None
    return int(text)


def _is_within_fiscal_year_range(
    fiscal_year: str,
    fiscal_year_start: int | None,
    fiscal_year_end: int | None,
) -> bool:
    if fiscal_year_start is None or fiscal_year_end is None:
        return True
    parsed_year = _parse_fiscal_year(fiscal_year)
    if parsed_year is None:
        return False
    return fiscal_year_start <= parsed_year <= fiscal_year_end


def _filter_det_rows_by_fiscal_year(
    det_rows: list[dict[str, Any]],
    fallback_year: str,
    fiscal_year_start: int | None,
    fiscal_year_end: int | None,
) -> list[dict[str, Any]]:
    if fiscal_year_start is None or fiscal_year_end is None:
        return det_rows
    filtered_rows: list[dict[str, Any]] = []
    for row in det_rows:
        resolved_year = _normalize_compensation_year(str(row.get("year", "") or ""), fallback_year)
        if _is_within_fiscal_year_range(resolved_year, fiscal_year_start, fiscal_year_end):
            filtered_rows.append(row)
    return filtered_rows


def _locate_grants_table(blocks: list[BaseBlock]) -> tuple[TableBlock | None, HeadingBlock | None]:
    """Locate the Grants of Plan-Based Awards table and heading."""

    def _table_prefix_text(table: TableBlock, max_rows: int = 10) -> str:
        if not table.rows:
            return ""
        parts: list[str] = []
        for row in table.rows[: min(max_rows, len(table.rows))]:
            parts.extend(cell.strip() for cell in row if cell.strip())
        return " | ".join(parts).lower()

    def _table_header_text(table: TableBlock) -> str:
        if not table.rows:
            return ""
        scan_rows = min(10, len(table.rows))
        hint_terms = _GRANTS_REQUIRED_SCORE_TERMS | _GRANTS_HEADER_HINTS | _GRANTS_GRANT_TYPE_HINTS | {"name"}
        scored_rows: list[tuple[int, int, list[str]]] = []
        for row_index in range(scan_rows):
            cells = [cell.strip() for cell in table.rows[row_index] if cell.strip()]
            if not cells:
                continue
            row_text = " | ".join(cells).lower()
            alpha_cells = sum(1 for cell in cells if any(char.isalpha() for char in cell))
            digit_cells = sum(1 for cell in cells if any(char.isdigit() for char in cell))
            score = (alpha_cells * 2) - digit_cells
            if len(cells) >= 5:
                score += 2
            if any(term in row_text for term in hint_terms):
                score += 10
            if "table of contents" in row_text:
                score -= 4
            scored_rows.append((score, row_index, cells))

        if not scored_rows:
            return ""
        scored_rows.sort(key=lambda item: (item[0], -item[1]), reverse=True)
        selected = sorted(scored_rows[:5], key=lambda item: item[1])
        parts: list[str] = []
        for _, _, cells in selected:
            parts.extend(cells)
        return " | ".join(parts).lower()

    def _grants_schema_fit(header_text: str, prefix_text: str) -> bool:
        combined = f"{header_text} | {prefix_text}"
        tokens = set(re.findall(r"[a-z0-9]+", combined))

        def _has_all(*required: str) -> bool:
            return all(token in tokens for token in required)

        has_grant_date = ("grant date" in combined) or _has_all("grant", "date")

        has_non_equity_triplet = (
            ("non-equity incentive plan award" in combined)
            or ("non equity incentive plan award" in combined)
            or (
                _has_all("non", "equity", "incentive", "plan")
                and ("award" in tokens or "awards" in tokens)
            )
        )
        has_equity_triplet = (
            ("equity incentive plan award" in combined)
            or (
                _has_all("equity", "incentive", "plan")
                and ("award" in tokens or "awards" in tokens)
            )
        )
        has_threshold_target = _has_all("threshold", "target")
        has_payout_structure = has_non_equity_triplet or has_equity_triplet or has_threshold_target

        has_grants_columns = any(
            (
                ("award type" in combined) or _has_all("award", "type") or _has_all("awards", "type"),
                ("all other stock awards" in combined)
                or (_has_all("all", "other", "stock") and ("award" in tokens or "awards" in tokens)),
                ("all other option awards" in combined)
                or (_has_all("all", "other", "option") and ("award" in tokens or "awards" in tokens)),
                ("exercise or base price" in combined)
                or (_has_all("exercise", "price") and ("base" in tokens or "option" in tokens)),
                ("grant date fair value" in combined) or (_has_all("fair", "value") and "grant" in tokens),
            )
        )
        return has_grant_date and has_payout_structure and has_grants_columns

    def _score_grants_table_candidate(table: TableBlock, heading_text: str) -> int:
        header_text = _table_header_text(table)
        prefix_text = _table_prefix_text(table)
        body_text = table.linearized_text.lower()
        if not header_text and not prefix_text:
            return -10

        row_count = len(table.rows)
        col_count = max((len(row) for row in table.rows), default=0)
        header_rows = table.header_row_count if table.header_row_count > 0 else min(4, row_count)
        header_rows = min(max(1, header_rows), row_count) if row_count else 0
        data_row_count = max(0, row_count - header_rows)

        combined = f"{header_text} | {prefix_text}"
        score = 0
        if any(term in combined for term in _GRANTS_REQUIRED_SCORE_TERMS):
            score += 4
        if any(term in body_text for term in _GRANTS_REQUIRED_SCORE_TERMS):
            score += 2
        if any(hint in combined for hint in _GRANTS_HEADER_HINTS):
            score += 4
        if any(hint in body_text for hint in _GRANTS_GRANT_TYPE_HINTS):
            score += 3
        if row_count >= 4:
            score += 2
        if col_count >= 8:
            score += 2
        if data_row_count <= 1:
            score -= 4
        if col_count <= 3:
            score -= 5

        heading_lc = heading_text.lower()
        if any(signature in heading_lc for signature in _GRANTS_SIGNATURES):
            score += 3

        if any(hint in combined for hint in _COMP_REJECT_HEADER_HINTS):
            score -= 6
        if "beneficial ownership" in combined:
            score -= 10
        if not _grants_schema_fit(header_text, prefix_text):
            score -= 12

        return score

    grants_headings: list[tuple[int, HeadingBlock]] = [
        (index, block)
        for index, block in enumerate(blocks)
        if isinstance(block, HeadingBlock)
        and any(signature in block.text.lower() for signature in _GRANTS_SIGNATURES)
    ]
    grants_contexts: list[tuple[int, str, HeadingBlock | None]] = [
        (index, heading.text, heading) for index, heading in grants_headings
    ]
    for index, block in enumerate(blocks):
        if not isinstance(block, ProseBlock):
            continue
        prose_text = block.text.strip()
        prose_lc = prose_text.lower()
        if not prose_text:
            continue
        if "table of contents" in prose_lc:
            continue
        if len(prose_text) > 180:
            continue
        if any(signature in prose_lc for signature in _GRANTS_SIGNATURES):
            grants_contexts.append((index, prose_text, None))

    scored_candidates: list[tuple[int, int, int, TableBlock, HeadingBlock | None]] = []
    for context_index, context_text, context_heading in grants_contexts:
        if context_heading is not None:
            for block in blocks:
                if not isinstance(block, TableBlock) or block.section_id != context_heading.id:
                    continue
                score = _score_grants_table_candidate(block, context_text)
                if score >= 8:
                    scored_candidates.append((score, 1, 0, block, context_heading))

        for offset in range(1, 16):
            candidate_index = context_index + offset
            if candidate_index >= len(blocks):
                break
            candidate = blocks[candidate_index]
            if not isinstance(candidate, TableBlock):
                continue
            score = _score_grants_table_candidate(candidate, context_text)
            if score >= 8:
                proximity_boost = 1 if context_heading is not None else 0
                scored_candidates.append((score, proximity_boost, -offset, candidate, context_heading))

    if scored_candidates:
        best = max(scored_candidates, key=lambda item: (item[0], item[1], item[2]))
        return best[3], best[4]

    global_candidates: list[tuple[int, TableBlock]] = []
    for block in blocks:
        if not isinstance(block, TableBlock):
            continue
        score = _score_grants_table_candidate(block, "")
        if score >= 10:
            global_candidates.append((score, block))
    if global_candidates:
        best_global = max(global_candidates, key=lambda item: item[0])
        return best_global[1], None
    return None, None


def _locate_outstanding_equity_awards_table(
    blocks: list[BaseBlock],
) -> tuple[TableBlock | None, HeadingBlock | None]:
    """Locate the Outstanding Equity Awards at Fiscal Year-End table and heading."""

    def _table_prefix_text(table: TableBlock, max_rows: int = 12) -> str:
        if not table.rows:
            return ""
        parts: list[str] = []
        for row in table.rows[: min(max_rows, len(table.rows))]:
            parts.extend(cell.strip() for cell in row if cell.strip())
        return " | ".join(parts).lower()

    def _table_header_text(table: TableBlock) -> str:
        if not table.rows:
            return ""
        scan_rows = min(12, len(table.rows))
        hint_terms = _OUTSTANDING_EQUITY_REQUIRED_TERMS | _OUTSTANDING_EQUITY_HEADER_HINTS | {"name"}
        scored_rows: list[tuple[int, int, list[str]]] = []
        for row_index in range(scan_rows):
            cells = [cell.strip() for cell in table.rows[row_index] if cell.strip()]
            if not cells:
                continue
            row_text = " | ".join(cells).lower()
            alpha_cells = sum(1 for cell in cells if any(char.isalpha() for char in cell))
            digit_cells = sum(1 for cell in cells if any(char.isdigit() for char in cell))
            score = (alpha_cells * 2) - digit_cells
            if len(cells) >= 5:
                score += 2
            if any(term in row_text for term in hint_terms):
                score += 10
            if "table of contents" in row_text:
                score -= 4
            if any(hint in row_text for hint in _COMP_REJECT_HEADER_HINTS):
                score -= 6
            scored_rows.append((score, row_index, cells))
        if not scored_rows:
            return ""
        scored_rows.sort(key=lambda item: (item[0], -item[1]), reverse=True)
        selected = sorted(scored_rows[:6], key=lambda item: item[1])
        parts: list[str] = []
        for _, _, cells in selected:
            parts.extend(cells)
        return " | ".join(parts).lower()

    def _equity_schema_fit(header_text: str, prefix_text: str) -> bool:
        combined = f"{header_text} | {prefix_text}"
        tokens = set(re.findall(r"[a-z0-9]+", combined))

        def _has_all(*required: str) -> bool:
            return all(token in tokens for token in required)

        has_name = "name" in tokens
        has_grant_date = ("grant date" in combined) or _has_all("grant", "date")
        has_option_axis = (
            ("unexercised options" in combined)
            or (
                ("option" in tokens or "options" in tokens)
                and ("exercisable" in tokens or "unexercisable" in tokens)
            )
        )
        has_stock_axis = (
            ("have not vested" in combined and ("shares" in tokens or "units" in tokens))
            or (_has_all("stock", "not", "vested"))
        )
        has_pricing_or_expiry = (
            ("option exercise price" in combined)
            or _has_all("exercise", "price")
            or ("option expiration date" in combined)
            or _has_all("expiration", "date")
        )
        has_equity_incentive = (
            "equity incentive plan awards" in combined
            or _has_all("equity", "incentive", "plan")
        )
        return (
            has_name
            and has_option_axis
            and has_stock_axis
            and has_pricing_or_expiry
            and (has_grant_date or has_equity_incentive)
        )

    def _score_equity_table_candidate(table: TableBlock, context_text: str) -> int:
        header_text = _table_header_text(table)
        prefix_text = _table_prefix_text(table)
        body_text = table.linearized_text.lower()
        if not header_text and not prefix_text:
            return -10

        row_count = len(table.rows)
        col_count = max((len(row) for row in table.rows), default=0)
        header_rows = table.header_row_count if table.header_row_count > 0 else min(6, row_count)
        header_rows = min(max(1, header_rows), row_count) if row_count else 0
        data_row_count = max(0, row_count - header_rows)
        combined = f"{header_text} | {prefix_text}"

        score = 0
        if any(term in combined for term in _OUTSTANDING_EQUITY_REQUIRED_TERMS):
            score += 4
        if any(term in body_text for term in _OUTSTANDING_EQUITY_REQUIRED_TERMS):
            score += 2
        if any(hint in combined for hint in _OUTSTANDING_EQUITY_HEADER_HINTS):
            score += 4
        if any(hint in body_text for hint in _OUTSTANDING_EQUITY_HEADER_HINTS):
            score += 2
        if row_count >= 4:
            score += 2
        if col_count >= 9:
            score += 2
        if data_row_count <= 1:
            score -= 4
        if col_count <= 4:
            score -= 6

        context_lc = context_text.lower()
        if any(signature in context_lc for signature in _OUTSTANDING_EQUITY_SIGNATURES):
            score += 3
        if any(hint in combined for hint in _COMP_REJECT_HEADER_HINTS):
            score -= 6
        if "beneficial ownership" in combined:
            score -= 10
        if not _equity_schema_fit(header_text, prefix_text):
            score -= 12
        return score

    contexts: list[tuple[int, str, HeadingBlock | None]] = []
    for index, block in enumerate(blocks):
        if isinstance(block, HeadingBlock):
            heading_lc = block.text.lower()
            if any(signature in heading_lc for signature in _OUTSTANDING_EQUITY_SIGNATURES):
                contexts.append((index, block.text, block))
        elif isinstance(block, ProseBlock):
            prose_text = block.text.strip()
            prose_lc = prose_text.lower()
            if not prose_text or "table of contents" in prose_lc:
                continue
            if len(prose_text) > 240:
                continue
            if any(signature in prose_lc for signature in _OUTSTANDING_EQUITY_SIGNATURES):
                contexts.append((index, prose_text, None))

    scored_candidates: list[tuple[int, int, int, TableBlock, HeadingBlock | None]] = []
    for context_index, context_text, context_heading in contexts:
        if context_heading is not None:
            for block in blocks:
                if not isinstance(block, TableBlock) or block.section_id != context_heading.id:
                    continue
                score = _score_equity_table_candidate(block, context_text)
                if score >= 8:
                    scored_candidates.append((score, 1, 0, block, context_heading))

        for offset in range(1, 19):
            candidate_index = context_index + offset
            if candidate_index >= len(blocks):
                break
            candidate = blocks[candidate_index]
            if not isinstance(candidate, TableBlock):
                continue
            score = _score_equity_table_candidate(candidate, context_text)
            if score >= 8:
                proximity_boost = 1 if context_heading is not None else 0
                scored_candidates.append((score, proximity_boost, -offset, candidate, context_heading))

    if scored_candidates:
        best = max(scored_candidates, key=lambda item: (item[0], item[1], item[2]))
        return best[3], best[4]

    global_candidates: list[tuple[int, TableBlock]] = []
    for block in blocks:
        if not isinstance(block, TableBlock):
            continue
        score = _score_equity_table_candidate(block, "")
        if score >= 10:
            global_candidates.append((score, block))
    if global_candidates:
        best_global = max(global_candidates, key=lambda item: item[0])
        return best_global[1], None
    return None, None


def _det_rows_have_grants_payload(det_rows: list[dict[str, Any]]) -> bool:
    """Return True when deterministic grant rows contain at least one usable value."""
    grant_fields = (
        "non_equity_threshold",
        "non_equity_target",
        "non_equity_maximum",
        "equity_threshold",
        "equity_target",
        "equity_maximum",
        "all_other_stock_awards_shares",
        "all_other_option_awards_securities",
        "exercise_or_base_price",
        "grant_date_fair_value",
    )
    empty_markers = {"", "-", "—", "$", "n/a", "na", "none"}

    for row in det_rows:
        for field in grant_fields:
            value = row.get(field)
            if isinstance(value, (int, float)):
                return True
            text = str(value or "").strip().lower()
            if not text or text in empty_markers:
                continue
            if any(char.isdigit() for char in text):
                return True
    return False


def _llm_result_has_grants_payload(result: CompanyGrantsResult) -> bool:
    for row in result.rows:
        values = [
            row.name.strip(),
            row.grant_date or "",
            row.non_equity_threshold or "",
            row.non_equity_target or "",
            row.non_equity_maximum or "",
            row.equity_threshold or "",
            row.equity_target or "",
            row.equity_maximum or "",
            row.all_other_stock_awards_shares or "",
            row.all_other_option_awards_securities or "",
            row.exercise_or_base_price or "",
            row.grant_date_fair_value or "",
        ]
        if any(str(value).strip() for value in values):
            return True
    return False


def _grant_row_from_det(row: dict[str, Any]) -> dict[str, Any]:
    name = str(row.get("exec_name", "") or "").strip()
    raw_type = str(row.get("grant_type", "") or "").strip()
    return {
        "Name": name,
        "Grant Type": raw_type,
        "Grant Date": _as_text(row.get("grant_date", "")),
        "Estimated future payouts under non-equity incentive plan awards (Threshold)": _as_text(
            row.get("non_equity_threshold", "")
        ),
        "Estimated future payouts under non-equity incentive plan awards (Target)": _as_text(
            row.get("non_equity_target", "")
        ),
        "Estimated future payouts under non-equity incentive plan awards (Maximum)": _as_text(
            row.get("non_equity_maximum", "")
        ),
        "Estimated future payouts under equity incentive plan awards (Threshold)": _as_text(
            row.get("equity_threshold", "")
        ),
        "Estimated future payouts under equity incentive plan awards (Target)": _as_text(
            row.get("equity_target", "")
        ),
        "Estimated future payouts under equity incentive plan awards (Maximum)": _as_text(
            row.get("equity_maximum", "")
        ),
        "All other stock awards: Number of shares of stock or units": _as_text(
            row.get("all_other_stock_awards_shares", "")
        ),
        "All other option awards: Number of securities underlying options": _as_text(
            row.get("all_other_option_awards_securities", "")
        ),
        "Exercise or base price of option awards": _as_text(row.get("exercise_or_base_price", "")),
        "Grant date fair value of stock and option awards": _as_text(row.get("grant_date_fair_value", "")),
    }


def _grant_row_from_llm(row: GrantPlanAwardRecord) -> dict[str, Any]:
    return {
        "Name": row.name,
        "Grant Type": row.grant_type,
        "Grant Date": row.grant_date or "",
        "Estimated future payouts under non-equity incentive plan awards (Threshold)": row.non_equity_threshold or "",
        "Estimated future payouts under non-equity incentive plan awards (Target)": row.non_equity_target or "",
        "Estimated future payouts under non-equity incentive plan awards (Maximum)": row.non_equity_maximum or "",
        "Estimated future payouts under equity incentive plan awards (Threshold)": row.equity_threshold or "",
        "Estimated future payouts under equity incentive plan awards (Target)": row.equity_target or "",
        "Estimated future payouts under equity incentive plan awards (Maximum)": row.equity_maximum or "",
        "All other stock awards: Number of shares of stock or units": row.all_other_stock_awards_shares or "",
        "All other option awards: Number of securities underlying options": row.all_other_option_awards_securities or "",
        "Exercise or base price of option awards": row.exercise_or_base_price or "",
        "Grant date fair value of stock and option awards": row.grant_date_fair_value or "",
    }


def _det_rows_have_outstanding_equity_payload(det_rows: list[dict[str, Any]]) -> bool:
    """Return True when deterministic equity-awards rows contain usable values."""
    equity_fields = (
        "options_exercisable",
        "options_unexercisable",
        "equity_incentive_unearned_options",
        "option_exercise_price",
        "stock_unvested_shares",
        "stock_unvested_value",
        "equity_incentive_unearned_shares",
        "equity_incentive_unearned_value",
    )
    empty_markers = {"", "-", "—", "$", "n/a", "na", "none"}

    for row in det_rows:
        for field in equity_fields:
            value = row.get(field)
            if isinstance(value, (int, float)):
                return True
            text = str(value or "").strip().lower()
            if not text or text in empty_markers:
                continue
            if any(char.isdigit() for char in text):
                return True
    return False


def _llm_result_has_outstanding_equity_payload(result: CompanyOutstandingEquityAwardsResult) -> bool:
    for row in result.rows:
        values = [
            row.name.strip(),
            row.grant_date or "",
            row.options_exercisable or "",
            row.options_unexercisable or "",
            row.equity_incentive_unearned_options or "",
            row.option_exercise_price or "",
            row.option_expiration_date or "",
            row.stock_unvested_shares or "",
            row.stock_unvested_value or "",
            row.equity_incentive_unearned_shares or "",
            row.equity_incentive_unearned_value or "",
        ]
        if any(str(value).strip() for value in values):
            return True
    return False


def _outstanding_equity_row_from_det(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "Name": str(row.get("exec_name", "") or "").strip(),
        "Grant Date": _as_text(row.get("grant_date", row.get("option_grant_date", ""))),
        "Number of Securities Underlying Unexercised Options Exercisable (#)": _as_text(
            row.get("options_exercisable", "")
        ),
        "Number of Securities Underlying Unexercised Options Unexercisable (#)": _as_text(
            row.get("options_unexercisable", "")
        ),
        "Equity Incentive Plan Awards: Number of Securities Underlying Unexercised Unearned Options (#)": _as_text(
            row.get("equity_incentive_unearned_options", "")
        ),
        "Option Exercise Price ($)": _as_text(row.get("option_exercise_price", row.get("exercise_price", ""))),
        "Option Expiration Date": _as_text(row.get("option_expiration_date", row.get("expiration_date", ""))),
        "Number of Shares or Units of Stock that Have Not Vested (#)": _as_text(
            row.get("stock_unvested_shares", row.get("stock_awards_unvested_shares", ""))
        ),
        "Market Value of Shares or Units of Stock that Have Not Vested ($)": _as_text(
            row.get("stock_unvested_value", row.get("stock_awards_unvested_value", ""))
        ),
        "Equity Incentive Plan Awards: Number of Unearned Shares, Units, or Other Rights that Have Not Vested (#)": _as_text(
            row.get("equity_incentive_unearned_shares", "")
        ),
        "Equity Incentive Plan Awards: Market or Payout Value of Unearned Shares, Units, or Other Rights that Have Not Vested ($)": _as_text(
            row.get("equity_incentive_unearned_value", "")
        ),
    }


def _outstanding_equity_row_from_llm(row: OutstandingEquityAwardRecord) -> dict[str, Any]:
    return {
        "Name": row.name,
        "Grant Date": row.grant_date or "",
        "Number of Securities Underlying Unexercised Options Exercisable (#)": row.options_exercisable or "",
        "Number of Securities Underlying Unexercised Options Unexercisable (#)": row.options_unexercisable or "",
        "Equity Incentive Plan Awards: Number of Securities Underlying Unexercised Unearned Options (#)": row.equity_incentive_unearned_options
        or "",
        "Option Exercise Price ($)": row.option_exercise_price or "",
        "Option Expiration Date": row.option_expiration_date or "",
        "Number of Shares or Units of Stock that Have Not Vested (#)": row.stock_unvested_shares or "",
        "Market Value of Shares or Units of Stock that Have Not Vested ($)": row.stock_unvested_value or "",
        "Equity Incentive Plan Awards: Number of Unearned Shares, Units, or Other Rights that Have Not Vested (#)": row.equity_incentive_unearned_shares
        or "",
        "Equity Incentive Plan Awards: Market or Payout Value of Unearned Shares, Units, or Other Rights that Have Not Vested ($)": row.equity_incentive_unearned_value
        or "",
    }


def _normalize_compensation_year(raw_year: str, fallback_year: str) -> str:
    if re.fullmatch(r"\d{4}", raw_year.strip()):
        return raw_year.strip()
    if re.fullmatch(r"\d{4}", fallback_year.strip()):
        return fallback_year.strip()
    return "unknown"


def _compensation_row_from_det(
    *,
    row: dict[str, Any],
    cik: str,
    company_name: str,
    filing_url: str,
    ticker: str,
    fallback_year: str,
) -> dict[str, str]:
    resolved_year = _normalize_compensation_year(str(row.get("year", "") or ""), fallback_year)
    return {
        "CIK": cik,
        "Company Name": company_name,
        "Filing URL": filing_url,
        "ticker": ticker,
        "Name": str(row.get("exec_name", "") or "").strip(),
        "Title": str(row.get("exec_title", "") or "").strip(),
        "Year": resolved_year,
        "Salary ($)": _as_digit_string(row.get("salary")),
        "Bonus Awards ($)": _as_digit_string(row.get("bonus")),
        "Stock Awards ($)": _as_digit_string(row.get("stock_awards")),
        "Option Awards ($)": _as_digit_string(row.get("option_awards")),
        "Non-Equity Incentive Plan Compensation ($)": _as_digit_string(row.get("non_equity_incentive")),
        "Change in pension value and nonqualified deferred compensation earnings ($)": _as_digit_string(
            row.get("pension_change")
        ),
        "All Other Compensation ($)": _as_digit_string(row.get("other_comp")),
        "Total ($)": _as_digit_string(row.get("total")),
        "Extra information": _as_text(row.get("footnote_refs", "")).strip(),
        "__cik": cik,
        "__year": resolved_year,
    }


def _compensation_row_from_llm(
    *,
    record: ExecCompRecord,
    cik: str,
    company_name: str,
    filing_url: str,
    ticker: str,
    fallback_year: str,
) -> dict[str, str]:
    resolved_year = _normalize_compensation_year(record.fiscal_year, fallback_year)
    return {
        "CIK": cik,
        "Company Name": company_name,
        "Filing URL": filing_url,
        "ticker": ticker,
        "Name": record.name.strip(),
        "Title": record.title.strip(),
        "Year": resolved_year,
        "Salary ($)": record.salary or "",
        "Bonus Awards ($)": record.bonus or "",
        "Stock Awards ($)": record.stock_awards or "",
        "Option Awards ($)": record.option_awards or "",
        "Non-Equity Incentive Plan Compensation ($)": record.non_equity_incentive or "",
        "Change in pension value and nonqualified deferred compensation earnings ($)": record.pension_change or "",
        "All Other Compensation ($)": record.other_comp or "",
        "Total ($)": record.total or "",
        "Extra information": (record.footnotes or "").strip(),
        "__cik": cik,
        "__year": resolved_year,
    }


def _llm_record_to_det_row(record: ExecCompRecord, fallback_year: str) -> dict[str, Any]:
    """Convert an LLM compensation record to deterministic row-like shape."""
    return {
        "exec_name": record.name.strip(),
        "exec_title": record.title.strip(),
        "year": _normalize_compensation_year(record.fiscal_year, fallback_year),
        "salary": record.salary or "",
        "bonus": record.bonus or "",
        "stock_awards": record.stock_awards or "",
        "option_awards": record.option_awards or "",
        "non_equity_incentive": record.non_equity_incentive or "",
        "pension_change": record.pension_change or "",
        "other_comp": record.other_comp or "",
        "total": record.total or "",
        "footnote_refs": (record.footnotes or "").strip(),
    }


def _compensation_row_has_payload(row: dict[str, str]) -> bool:
    values = [
        row.get("Name", "").strip(),
        row.get("Title", "").strip(),
        row.get("Salary ($)", "").strip(),
        row.get("Bonus Awards ($)", "").strip(),
        row.get("Stock Awards ($)", "").strip(),
        row.get("Option Awards ($)", "").strip(),
        row.get("Non-Equity Incentive Plan Compensation ($)", "").strip(),
        row.get("Change in pension value and nonqualified deferred compensation earnings ($)", "").strip(),
        row.get("All Other Compensation ($)", "").strip(),
        row.get("Total ($)", "").strip(),
        row.get("Extra information", "").strip(),
    ]
    return any(values)


def process_cik(
    cik: str,
    model: str,
    skip_db: bool,
    fiscal_year_start: int | None = None,
    fiscal_year_end: int | None = None,
    filing_override: fetcher.FetchedFiling | None = None,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], list[dict[str, str]], list[dict[str, Any]]]:
    """
    Full pipeline for one CIK.
    Returns (key_results_row, log_row, grants_rows, compensation_rows, outstanding_equity_rows).

    This function never raises. All errors are caught and returned
    as a failed row so the batch loop stays clean.

    Pipeline stages:
      acquire -> parse -> locate -> extract (det or llm) -> collapse -> build rows
    """
    t0 = time.monotonic()
    grants_rows_out: list[dict[str, Any]] = []
    compensation_rows_out: list[dict[str, str]] = []
    outstanding_equity_rows_out: list[dict[str, Any]] = []
    source_filing_date = ""
    source_accession_number = ""
    source_filing_url = ""
    source_ticker = ""

    def _failed(
        reason: str,
        company_name: str = "",
        extra: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], list[dict[str, str]], list[dict[str, Any]]]:
        elapsed = round(time.monotonic() - t0, 2)
        base: dict[str, Any] = {col: "" for col in KEY_RESULTS_COLUMNS}
        base.update(
            {
                "cik": cik,
                "company_name": company_name,
                "ticker": source_ticker,
                "filing_date": source_filing_date,
                "accession_number": source_accession_number,
                "filing_url": source_filing_url,
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
            "source_filing_date": source_filing_date,
            "source_accession_number": source_accession_number,
            "source_filing_url": source_filing_url,
            "status": "failed",
            "extraction_method": "failed",
            "block_count": 0,
            "table_count": 0,
            "comp_heading_found": False,
            "comp_table_found": False,
            "grant_table_found": False,
            "det_rows": 0,
            "llm_confidence": "",
            "cda_token_count": 0,
            "pay_for_performance_flag": False,
            "elapsed_seconds": elapsed,
            "error": reason,
        }
        if extra:
            for key, value in extra.items():
                if key in KEY_RESULTS_COLUMNS:
                    base[key] = value
                if key in BATCH_LOG_COLUMNS:
                    log_row[key] = value
        return base, log_row, grants_rows_out, compensation_rows_out, outstanding_equity_rows_out

    if filing_override is not None:
        filing = filing_override
    else:
        try:
            filing = _fetch_latest_def14a(cik)
        except Exception as exc:  # noqa: BLE001
            return _failed(f"acquisition_failed: {exc}")

    company_name = str(getattr(filing, "company_name", "") or "")
    ticker = str(getattr(filing, "ticker", "") or "")
    source_filing_date = str(getattr(filing, "filing_date", "") or "")
    source_accession_number = str(getattr(filing, "accession_number", "") or "")
    source_filing_url = str(getattr(filing, "filing_url", "") or "")
    source_ticker = ticker

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

    grants_table, _ = _locate_grants_table(blocks)
    grant_table_found = grants_table is not None
    grants_det_rows: list[dict[str, Any]] = []
    try:
        if grants_table is not None:
            grants_det_rows = det_extractor.extract_grants_plan_based(
                blocks,
                meta_dict,
                selected_table=grants_table,
            )
        else:
            grants_det_rows = det_extractor.extract_grants_plan_based(blocks, meta_dict)
    except Exception as grants_exc:  # noqa: BLE001
        log.warning("grants_det_extract_failed | cik=%s error=%s", cik, grants_exc)

    grants_fiscal_year = _infer_fiscal_year_from_filing_date(filing.filing_date)
    include_grants = _is_within_fiscal_year_range(
        grants_fiscal_year,
        fiscal_year_start,
        fiscal_year_end,
    )
    if include_grants:
        if grants_det_rows and _det_rows_have_grants_payload(grants_det_rows):
            for det_row in grants_det_rows:
                output_row = _grant_row_from_det(det_row)
                output_row["CIK"] = cik
                output_row["Company Name"] = company_name
                output_row["Filing URL"] = filing.filing_url
                output_row["__cik"] = cik
                output_row["__fiscal_year"] = grants_fiscal_year
                grants_rows_out.append(output_row)
        elif grants_table is not None:
            llm_grants_result = extract_grants_from_plan_based_table(
                company_name=company_name,
                cik=cik,
                filing_date=str(filing.filing_date or ""),
                accession_number=filing.accession_number,
                table_text=grants_table.linearized_text,
                model=model,
            )
            if _llm_result_has_grants_payload(llm_grants_result):
                for llm_row in llm_grants_result.rows:
                    output_row = _grant_row_from_llm(llm_row)
                    output_row["CIK"] = cik
                    output_row["Company Name"] = company_name
                    output_row["Filing URL"] = filing.filing_url
                    output_row["__cik"] = cik
                    output_row["__fiscal_year"] = grants_fiscal_year
                    grants_rows_out.append(output_row)

    outstanding_equity_table, _ = _locate_outstanding_equity_awards_table(blocks)
    outstanding_equity_det_rows: list[dict[str, Any]] = []
    try:
        if outstanding_equity_table is not None:
            outstanding_equity_det_rows = det_extractor.extract_equity_awards(
                blocks,
                meta_dict,
                selected_table=outstanding_equity_table,
            )
        else:
            outstanding_equity_det_rows = det_extractor.extract_equity_awards(blocks, meta_dict)
    except Exception as equity_exc:  # noqa: BLE001
        log.warning("outstanding_equity_det_extract_failed | cik=%s error=%s", cik, equity_exc)

    outstanding_equity_fiscal_year = _infer_fiscal_year_from_filing_date(filing.filing_date)
    include_outstanding_equity = _is_within_fiscal_year_range(
        outstanding_equity_fiscal_year,
        fiscal_year_start,
        fiscal_year_end,
    )
    if include_outstanding_equity:
        if outstanding_equity_det_rows and _det_rows_have_outstanding_equity_payload(outstanding_equity_det_rows):
            for det_row in outstanding_equity_det_rows:
                output_row = _outstanding_equity_row_from_det(det_row)
                output_row["CIK"] = cik
                output_row["Company Name"] = company_name
                output_row["Filing URL"] = filing.filing_url
                output_row["__cik"] = cik
                output_row["__fiscal_year"] = outstanding_equity_fiscal_year
                outstanding_equity_rows_out.append(output_row)
        elif outstanding_equity_table is not None:
            llm_outstanding_equity_result = extract_outstanding_equity_awards_table(
                company_name=company_name,
                cik=cik,
                filing_date=str(filing.filing_date or ""),
                accession_number=filing.accession_number,
                table_text=outstanding_equity_table.linearized_text,
                model=model,
            )
            if _llm_result_has_outstanding_equity_payload(llm_outstanding_equity_result):
                for llm_row in llm_outstanding_equity_result.rows:
                    output_row = _outstanding_equity_row_from_llm(llm_row)
                    output_row["CIK"] = cik
                    output_row["Company Name"] = company_name
                    output_row["Filing URL"] = filing.filing_url
                    output_row["__cik"] = cik
                    output_row["__fiscal_year"] = outstanding_equity_fiscal_year
                    outstanding_equity_rows_out.append(output_row)

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
        det_rows = _extract_summary_compensation_rows(blocks, meta_dict, comp_table)
    except Exception as exc:  # noqa: BLE001
        return _failed(
            f"deterministic_extract_failed: {exc}",
            company_name,
            {
                "block_count": block_count,
                "table_count": table_count,
                "comp_heading_found": comp_heading_found,
                "comp_table_found": comp_table_found,
                "grant_table_found": grant_table_found,
            },
        )

    inferred_comp_year = _infer_fiscal_year_from_filing_date(filing.filing_date)
    target_comp_year: int | None = None
    if (
        fiscal_year_start is not None
        and fiscal_year_end is not None
        and fiscal_year_start == fiscal_year_end
    ):
        target_comp_year = fiscal_year_start
    if det_rows and _det_rows_have_comp_payload(det_rows):
        det_rows = _filter_det_rows_by_fiscal_year(
            det_rows,
            inferred_comp_year,
            fiscal_year_start,
            fiscal_year_end,
        )
        if not det_rows:
            return _failed(
                "no_comp_rows_in_fiscal_year_range",
                company_name,
                {
                    "block_count": block_count,
                    "table_count": table_count,
                    "comp_heading_found": comp_heading_found,
                    "comp_table_found": comp_table_found,
                    "grant_table_found": grant_table_found,
                },
            )
        extraction_method = "deterministic"
        for det_row in det_rows:
            output_row = _compensation_row_from_det(
                row=det_row,
                cik=cik,
                company_name=company_name,
                filing_url=filing.filing_url,
                ticker=ticker,
                fallback_year=inferred_comp_year,
            )
            if _compensation_row_has_payload(output_row):
                compensation_rows_out.append(output_row)
        roles = _collapse_to_roles(det_rows)
    elif comp_table is not None:
        log.info(
            "llm_fallback triggered | cik=%s target_fiscal_year=%s",
            cik,
            target_comp_year if target_comp_year is not None else "most_recent",
        )
        llm_table_text = _build_llm_comp_table_text(comp_table, target_comp_year)
        llm_result = extract_company_comp_from_summary_table(
            company_name=company_name,
            cik=cik,
            filing_date=str(filing.filing_date or ""),
            accession_number=filing.accession_number,
            table_text=llm_table_text,
            target_fiscal_year=target_comp_year,
            model=model,
        )
        llm_confidence = llm_result.confidence
        llm_model_used = model
        if not _llm_result_has_comp_payload(llm_result):
            return _failed(
                "llm_extract_failed_empty_result",
                company_name,
                {
                    "block_count": block_count,
                    "table_count": table_count,
                    "comp_heading_found": comp_heading_found,
                    "comp_table_found": comp_table_found,
                    "grant_table_found": grant_table_found,
                    "llm_confidence": llm_confidence,
                },
            )
        extraction_method = "llm"
        llm_records: list[ExecCompRecord] = []
        if isinstance(getattr(llm_result, "rows", None), list):
            llm_records.extend(
                row for row in llm_result.rows if isinstance(row, ExecCompRecord)
            )
        if not llm_records:
            for role_key in ("ceo", "cfo", "coo", "other1", "other2"):
                role_record = getattr(llm_result, role_key, None)
                if isinstance(role_record, ExecCompRecord):
                    llm_records.append(role_record)

        llm_rows_in_range = 0
        llm_rows_for_roles: list[dict[str, Any]] = []
        for role_record in llm_records:
            resolved_year = _normalize_compensation_year(
                role_record.fiscal_year,
                inferred_comp_year,
            )
            if not _is_within_fiscal_year_range(resolved_year, fiscal_year_start, fiscal_year_end):
                continue
            output_row = _compensation_row_from_llm(
                record=role_record,
                cik=cik,
                company_name=company_name,
                filing_url=filing.filing_url,
                ticker=ticker,
                fallback_year=inferred_comp_year,
            )
            if _compensation_row_has_payload(output_row):
                compensation_rows_out.append(output_row)
                llm_rows_in_range += 1
                llm_rows_for_roles.append(_llm_record_to_det_row(role_record, inferred_comp_year))
        if llm_rows_in_range == 0:
            return _failed(
                "no_comp_rows_in_fiscal_year_range",
                company_name,
                {
                    "block_count": block_count,
                    "table_count": table_count,
                    "comp_heading_found": comp_heading_found,
                    "comp_table_found": comp_table_found,
                    "grant_table_found": grant_table_found,
                    "llm_confidence": llm_confidence,
                },
            )
        roles = _collapse_to_roles(llm_rows_for_roles)
    else:
        return _failed(
            "no_comp_table_located",
            company_name,
            {
                "block_count": block_count,
                "table_count": table_count,
                "comp_heading_found": comp_heading_found,
                "comp_table_found": comp_table_found,
                "grant_table_found": grant_table_found,
            },
        )

    fiscal_year_val = _role_fiscal_year(extraction_method, roles)

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

    if not result_row.get("ceo_name", "").strip() and not result_row.get("ceo_total", "").strip():
        return _failed(
            "ceo_role_unresolved",
            company_name,
            {
                "block_count": block_count,
                "table_count": table_count,
                "comp_heading_found": comp_heading_found,
                "comp_table_found": comp_table_found,
                "grant_table_found": grant_table_found,
                "llm_confidence": llm_confidence,
            },
        )

    if not _result_row_has_comp_payload(result_row):
        return _failed(
            "extraction_empty_after_mapping",
            company_name,
            {
                "block_count": block_count,
                "table_count": table_count,
                "comp_heading_found": comp_heading_found,
                "comp_table_found": comp_table_found,
                "grant_table_found": grant_table_found,
                "llm_confidence": llm_confidence,
            },
        )

    elapsed = round(time.monotonic() - t0, 2)
    log_row: dict[str, Any] = {
        "cik": cik,
        "company_name": company_name,
        "source_filing_date": str(filing.filing_date or ""),
        "source_accession_number": filing.accession_number,
        "source_filing_url": filing.filing_url,
        "status": "ok",
        "extraction_method": extraction_method,
        "block_count": block_count,
        "table_count": table_count,
        "comp_heading_found": comp_heading_found,
        "comp_table_found": comp_table_found,
        "grant_table_found": grant_table_found,
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

    return result_row, log_row, grants_rows_out, compensation_rows_out, outstanding_equity_rows_out


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
    parser.add_argument(
        "--fiscal-year-start",
        type=int,
        default=None,
        help=(
            "Inclusive start fiscal year for supplemental table outputs "
            "(must be paired with --fiscal-year-end; key_results.csv remains unchanged)"
        ),
    )
    parser.add_argument(
        "--fiscal-year-end",
        type=int,
        default=None,
        help=(
            "Inclusive end fiscal year for supplemental table outputs "
            "(must be paired with --fiscal-year-start; key_results.csv remains unchanged)"
        ),
    )
    args = parser.parse_args()

    has_start = args.fiscal_year_start is not None
    has_end = args.fiscal_year_end is not None
    if has_start != has_end:
        parser.error("Both --fiscal-year-start and --fiscal-year-end are required to enable year-range filtering.")
    fiscal_year_start: int | None = None
    fiscal_year_end: int | None = None
    if has_start and has_end:
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

    year_range_label = (
        f"{fiscal_year_start}-{fiscal_year_end}"
        if fiscal_year_start is not None and fiscal_year_end is not None
        else "all"
    )
    log.info(
        "batch start | label=%s ciks=%d model=%s fiscal_year_range=%s",
        args.batch_label,
        len(ciks),
        args.model,
        year_range_label,
    )

    out_dir = BATCH_OUTPUT_BASE / args.batch_label
    out_dir.mkdir(parents=True, exist_ok=True)
    key_results_path = out_dir / "key_results.csv"
    batch_log_path = out_dir / "batch_log.csv"
    batch_log_failed_path = out_dir / "batch_log_failed.csv"
    grants_master_path = out_dir / "grants_plan_based_master.csv"
    outstanding_equity_master_path = out_dir / "outstanding_equity_awards_master.csv"
    compensation_master_path = out_dir / "compensation_table_master.csv"
    grants_by_cik_year_dir = out_dir / "grants_plan_based_by_cik_year"
    outstanding_equity_by_cik_year_dir = out_dir / "outstanding_equity_awards_by_cik_year"
    compensation_by_cik_year_dir = out_dir / "compensation_by_cik_year"
    grants_by_cik_year_dir.mkdir(parents=True, exist_ok=True)
    outstanding_equity_by_cik_year_dir.mkdir(parents=True, exist_ok=True)
    compensation_by_cik_year_dir.mkdir(parents=True, exist_ok=True)

    success_count = 0
    failed_count = 0
    ceo_total_populated = 0
    supplemental_total_runs = 0
    supplemental_ok_runs = 0
    supplemental_failed_runs = 0
    supplemental_fetch_failed_runs = 0
    supplemental_skipped_runs = 0
    all_grants_rows: list[dict[str, Any]] = []
    all_outstanding_equity_rows: list[dict[str, Any]] = []
    all_compensation_rows: list[dict[str, str]] = []
    all_log_rows: list[dict[str, Any]] = []

    with (
        key_results_path.open("w", newline="", encoding="utf-8") as key_results_file,
        batch_log_path.open("w", newline="", encoding="utf-8") as batch_log_file,
        grants_master_path.open("w", newline="", encoding="utf-8") as grants_master_file,
        outstanding_equity_master_path.open("w", newline="", encoding="utf-8") as outstanding_equity_master_file,
        compensation_master_path.open("w", newline="", encoding="utf-8") as compensation_master_file,
    ):
        kr_writer = csv.DictWriter(key_results_file, fieldnames=KEY_RESULTS_COLUMNS)
        log_writer = csv.DictWriter(batch_log_file, fieldnames=BATCH_LOG_COLUMNS)
        grants_writer = csv.DictWriter(
            grants_master_file,
            fieldnames=GRANTS_OUTPUT_COLUMNS,
            extrasaction="ignore",
        )
        outstanding_equity_writer = csv.DictWriter(
            outstanding_equity_master_file,
            fieldnames=OUTSTANDING_EQUITY_AWARDS_OUTPUT_COLUMNS,
            extrasaction="ignore",
        )
        compensation_writer = csv.DictWriter(
            compensation_master_file,
            fieldnames=COMPENSATION_OUTPUT_COLUMNS,
            extrasaction="ignore",
        )
        kr_writer.writeheader()
        log_writer.writeheader()
        grants_writer.writeheader()
        outstanding_equity_writer.writeheader()
        compensation_writer.writeheader()

        for index, cik in enumerate(ciks, start=1):
            log.info("[%d/%d][Base] processing | cik=%s", index, len(ciks), cik)
            process_result = _call_process_cik(
                cik=cik,
                model=args.model,
                skip_db=args.no_db,
                fiscal_year_start=None,
                fiscal_year_end=None,
                filing_override=None,
            )
            if isinstance(process_result, tuple) and len(process_result) == 3:
                result_row, base_log_row_raw, grants_rows = process_result
                compensation_rows: list[dict[str, str]] = []
                outstanding_equity_rows: list[dict[str, Any]] = []
            else:
                if len(process_result) == 4:
                    result_row, base_log_row_raw, grants_rows, compensation_rows = process_result
                    outstanding_equity_rows = []
                else:
                    result_row, base_log_row_raw, grants_rows, compensation_rows, outstanding_equity_rows = process_result

            base_log_row = _enrich_log_row(
                base_log_row_raw,
                run_scope="base",
                filing=None,
                cik=cik,
                company_name=str(result_row.get("company_name", "") or ""),
            )

            grants_rows_to_write: list[dict[str, Any]] = []
            outstanding_equity_rows_to_write: list[dict[str, Any]] = []
            compensation_rows_to_write: list[dict[str, str]] = []
            supplemental_log_rows: list[dict[str, Any]] = []
            if fiscal_year_start is not None and fiscal_year_end is not None:
                total_years = fiscal_year_end - fiscal_year_start + 1
                for year_index, target_year in enumerate(range(fiscal_year_start, fiscal_year_end + 1), start=1):
                    supplemental_total_runs += 1
                    log.info(
                        "[%d/%d][Year%d/%d] supplemental year extract | cik=%s fiscal_year=%d",
                        index,
                        len(ciks),
                        year_index,
                        total_years,
                        cik,
                        target_year,
                    )
                    try:
                        year_filing = fetcher.fetch_def14a_for_fiscal_year(cik, target_year)
                    except Exception as year_fetch_exc:  # noqa: BLE001
                        supplemental_fetch_failed_runs += 1
                        year_fetch_error = str(year_fetch_exc)
                        missing_year_filing = "No DEF 14A found" in year_fetch_error
                        status = "skipped" if missing_year_filing else "failed"
                        if missing_year_filing:
                            supplemental_skipped_runs += 1
                            error_msg = f"supplemental_filing_not_found_for_year: {year_fetch_exc}"
                        else:
                            supplemental_failed_runs += 1
                            error_msg = f"supplemental_filing_fetch_failed: {year_fetch_exc}"
                        supplemental_log_rows.append(
                            _enrich_log_row(
                                raw_log_row={},
                                run_scope="supplemental",
                                target_fiscal_year=target_year,
                                filing=None,
                                override_status=status,
                                override_error=error_msg,
                                cik=cik,
                                company_name=str(result_row.get("company_name", "") or ""),
                            )
                        )
                        log.warning(
                            "[%d/%d][Year%d/%d] supplemental_filing_fetch_failed | cik=%s fiscal_year=%d error=%s",
                            index,
                            len(ciks),
                            year_index,
                            total_years,
                            cik,
                            target_year,
                            year_fetch_exc,
                        )
                        continue
                    year_process_result = _call_process_cik(
                        cik=cik,
                        model=args.model,
                        skip_db=True,  # Avoid repeated DB writes while sweeping year-specific table outputs.
                        fiscal_year_start=target_year,
                        fiscal_year_end=target_year,
                        filing_override=year_filing,
                    )
                    if isinstance(year_process_result, tuple) and len(year_process_result) == 3:
                        _, year_log_row_raw, year_grants_rows = year_process_result
                        year_compensation_rows: list[dict[str, str]] = []
                        year_outstanding_equity_rows: list[dict[str, Any]] = []
                    else:
                        if len(year_process_result) == 4:
                            _, year_log_row_raw, year_grants_rows, year_compensation_rows = year_process_result
                            year_outstanding_equity_rows = []
                        else:
                            (
                                _,
                                year_log_row_raw,
                                year_grants_rows,
                                year_compensation_rows,
                                year_outstanding_equity_rows,
                            ) = year_process_result
                    year_log_row = _enrich_log_row(
                        raw_log_row=year_log_row_raw,
                        run_scope="supplemental",
                        target_fiscal_year=target_year,
                        filing=year_filing,
                        cik=cik,
                    )
                    supplemental_log_rows.append(year_log_row)
                    log.info(
                        "[%d/%d][Year%d/%d] supplemental year result | cik=%s fiscal_year=%d status=%s method=%s grants_rows=%d compensation_rows=%d error=%s",
                        index,
                        len(ciks),
                        year_index,
                        total_years,
                        cik,
                        target_year,
                        str(year_log_row.get("status", "") or ""),
                        str(year_log_row.get("extraction_method", "") or ""),
                        len(year_grants_rows),
                        len(year_compensation_rows),
                        str(year_log_row.get("error", "") or ""),
                    )
                    if str(year_log_row.get("status", "")).lower() == "ok":
                        supplemental_ok_runs += 1
                    elif str(year_log_row.get("status", "")).lower() == "skipped":
                        supplemental_skipped_runs += 1
                    else:
                        supplemental_failed_runs += 1
                    grants_rows_to_write.extend(year_grants_rows)
                    outstanding_equity_rows_to_write.extend(year_outstanding_equity_rows)
                    compensation_rows_to_write.extend(year_compensation_rows)
            else:
                grants_rows_to_write = grants_rows
                outstanding_equity_rows_to_write = outstanding_equity_rows
                compensation_rows_to_write = compensation_rows

            kr_writer.writerow(result_row)
            log_writer.writerow(base_log_row)
            all_log_rows.append(dict(base_log_row))
            for supplemental_log_row in supplemental_log_rows:
                log_writer.writerow(supplemental_log_row)
                all_log_rows.append(dict(supplemental_log_row))
            for grants_row in grants_rows_to_write:
                grants_writer.writerow(grants_row)
                all_grants_rows.append(grants_row)
            for outstanding_equity_row in outstanding_equity_rows_to_write:
                outstanding_equity_writer.writerow(outstanding_equity_row)
                all_outstanding_equity_rows.append(outstanding_equity_row)
            for compensation_row in compensation_rows_to_write:
                compensation_writer.writerow(compensation_row)
                all_compensation_rows.append(compensation_row)
            key_results_file.flush()
            batch_log_file.flush()
            grants_master_file.flush()
            outstanding_equity_master_file.flush()
            compensation_master_file.flush()

            if result_row.get("status") == "ok":
                success_count += 1
                if result_row.get("ceo_total"):
                    ceo_total_populated += 1
            else:
                failed_count += 1

    grouped_grants_rows: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in all_grants_rows:
        row_cik = str(row.get("__cik", "") or "").strip()
        row_fiscal_year = str(row.get("__fiscal_year", "") or "").strip()
        if not row_cik:
            continue
        if not re.fullmatch(r"\d{4}", row_fiscal_year):
            row_fiscal_year = "unknown"
        grouped_grants_rows.setdefault((row_cik, row_fiscal_year), []).append(row)

    for (row_cik, row_fiscal_year), rows in grouped_grants_rows.items():
        per_file_path = grants_by_cik_year_dir / f"{row_cik}_{row_fiscal_year}.csv"
        with per_file_path.open("w", newline="", encoding="utf-8") as per_file:
            writer = csv.DictWriter(per_file, fieldnames=GRANTS_OUTPUT_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

    grouped_compensation_rows: dict[tuple[str, str], list[dict[str, str]]] = {}
    grouped_outstanding_equity_rows: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in all_outstanding_equity_rows:
        row_cik = str(row.get("__cik", "") or "").strip()
        row_fiscal_year = str(row.get("__fiscal_year", "") or "").strip()
        if not row_cik:
            continue
        if not re.fullmatch(r"\d{4}", row_fiscal_year):
            row_fiscal_year = "unknown"
        grouped_outstanding_equity_rows.setdefault((row_cik, row_fiscal_year), []).append(row)

    for (row_cik, row_fiscal_year), rows in grouped_outstanding_equity_rows.items():
        per_file_path = outstanding_equity_by_cik_year_dir / f"{row_cik}_{row_fiscal_year}.csv"
        with per_file_path.open("w", newline="", encoding="utf-8") as per_file:
            writer = csv.DictWriter(
                per_file,
                fieldnames=OUTSTANDING_EQUITY_AWARDS_OUTPUT_COLUMNS,
                extrasaction="ignore",
            )
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

    for row in all_compensation_rows:
        row_cik = str(row.get("__cik", "") or "").strip()
        row_year = str(row.get("__year", "") or "").strip()
        if not row_cik:
            continue
        if not re.fullmatch(r"\d{4}", row_year):
            row_year = "unknown"
        grouped_compensation_rows.setdefault((row_cik, row_year), []).append(row)

    for (row_cik, row_year), rows in grouped_compensation_rows.items():
        per_file_path = compensation_by_cik_year_dir / f"{row_cik}_{row_year}.csv"
        with per_file_path.open("w", newline="", encoding="utf-8") as per_file:
            writer = csv.DictWriter(per_file, fieldnames=COMPENSATION_OUTPUT_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            for row in rows:
                writer.writerow(row)

    failed_log_rows = [row for row in all_log_rows if str(row.get("status", "")).lower() == "failed"]
    with batch_log_failed_path.open("w", newline="", encoding="utf-8") as failed_log_file:
        failed_writer = csv.DictWriter(failed_log_file, fieldnames=BATCH_LOG_COLUMNS)
        failed_writer.writeheader()
        for row in failed_log_rows:
            failed_writer.writerow(row)

    log.info(
        "batch complete | base_success=%d base_failed=%d ceo_total_populated=%d/%d "
        "supplemental_total=%d supplemental_ok=%d supplemental_failed=%d supplemental_skipped=%d fetch_failed=%d",
        success_count,
        failed_count,
        ceo_total_populated,
        len(ciks),
        supplemental_total_runs,
        supplemental_ok_runs,
        supplemental_failed_runs,
        supplemental_skipped_runs,
        supplemental_fetch_failed_runs,
    )
    log.info("key_results -> %s", key_results_path)
    log.info("batch_log   -> %s", batch_log_path)
    log.info("batch_log_failed -> %s (rows=%d)", batch_log_failed_path, len(failed_log_rows))
    log.info("grants master -> %s", grants_master_path)
    log.info("grants by cik/year -> %s", grants_by_cik_year_dir)
    log.info("outstanding equity awards master -> %s", outstanding_equity_master_path)
    log.info("outstanding equity awards by cik/year -> %s", outstanding_equity_by_cik_year_dir)
    log.info("compensation master -> %s", compensation_master_path)
    log.info("compensation by cik/year -> %s", compensation_by_cik_year_dir)

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
