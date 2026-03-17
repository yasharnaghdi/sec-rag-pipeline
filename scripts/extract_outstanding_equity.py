"""
Standalone Outstanding Equity Awards extractor for DEF 14A filings.

Usage
-----
    poetry run python scripts/extract_outstanding_equity.py \
        --input fixtures/client_input.csv \
        --output output/outstanding_equity_awards.csv \
        --model gpt-5-mini \
        --limit 5 \
        --verbose

This script is decoupled from the batch50 pipeline so it can be
iterated on quickly. It uses raw HTML tables (not linearized text)
and OpenAI structured outputs for reliable extraction.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, Field, field_validator, model_validator

# ── project imports ─────────────────────────────────────────────
# Ensure project root is on sys.path so ingestion imports resolve.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from ingestion.edgar_folder_fetcher import fetch_latest_def14a  # noqa: E402
from ingestion.llm_comp_extractor import (  # noqa: E402
    CompanyOutstandingEquityAwardsResult,
    OutstandingEquityAwardRecord,
)
from ingestion.metadata_model import (  # noqa: E402
    BaseBlock,
    DocumentMetadata,
    HeadingBlock,
    ProseBlock,
    TableBlock,
)
from ingestion.sec_html_parser import SECHTMLParser  # noqa: E402

log = logging.getLogger(__name__)

# ── constants ───────────────────────────────────────────────────
DEFAULT_MODEL = "gpt-5-mini"
MAX_RAW_HTML_BYTES = 50_000  # fall back to linearized_text above this

OUTPUT_COLUMNS = [
    "CIK",
    "Company Name",
    "Filing URL",
    "Name",
    "Grant Date",
    "Option Award (Number of Securities Underlying Unexercised Options Exercisable (#))",
    "Option Award (Number of Securities Underlying Unexercised Options Unexercisable (#))",
    "Option Award (Equity Incentive Plan Awards: Number of Securities Underlying Unexercised Unearned Options (#))",
    "Option Exercise Price ($)",
    "Option Expiration Date",
    "Stock Awards (Number of Shares or Units of Stock that Have Not Vested (#))",
    "Stock Awards (Market Value of Shares or Units of Stock that Have Not Vested ($))",
    "Stock Awards (Equity Incentive Plan Awards: Number of Unearned Shares, Units, or Other Rights that Have Not Vested (#))",
    "Stock Awards (Equity Incentive Plan Awards: Market or Payout Value of Unearned Shares, Units, or Other Rights that Have Not Vested ($))",
]

# ── table locator constants (from run_batch50_key_results.py) ──
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

# ── table locator (copied from run_batch50_key_results.py:1616-1794) ──


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


# ── system prompt ───────────────────────────────────────────────
_SYSTEM_PROMPT = """\
You are a financial data extraction assistant specialised in SEC DEF 14A
proxy statement compensation tables.

You will receive an Outstanding Equity Awards at Fiscal Year-End table,
either as HTML markup or as linearized pipe-delimited text. Extract each
data row and return ONLY valid JSON matching the schema enforced by the
response_format.

EXTRACTION RULES:
1. If the input is HTML, use the <tr> and <td>/<th> structure to identify
   columns. Pay attention to colspan and rowspan attributes for merged cells
   and multi-level headers. If the input is pipe-delimited text, use the
   pipe separators and column positions to identify fields.
2. Preserve row granularity: one output row per source data row.
3. NAME PROPAGATION: In these tables, an executive's name appears once and
   spans multiple rows (via rowspan or simply appearing in the first row
   for that executive). If a data row has no name, assign the most recently
   seen executive name above it.
4. Field mapping is strict:
   - name <- Name column
   - grant_date <- Grant Date
   - options_exercisable <- Number of Securities Underlying Unexercised Options Exercisable (#)
   - options_unexercisable <- Number of Securities Underlying Unexercised Options Unexercisable (#)
   - equity_incentive_unearned_options <- Equity Incentive Plan Awards: Number of Securities Underlying Unexercised Unearned Options (#)
   - option_exercise_price <- Option Exercise Price ($)
   - option_expiration_date <- Option Expiration Date
   - stock_unvested_shares <- Number of Shares or Units of Stock that Have Not Vested (#)
   - stock_unvested_value <- Market Value of Shares or Units of Stock that Have Not Vested ($)
   - equity_incentive_unearned_shares <- Equity Incentive Plan Awards: Number of Unearned Shares, Units, or Other Rights that Have Not Vested (#)
   - equity_incentive_unearned_value <- Equity Incentive Plan Awards: Market or Payout Value of Unearned Shares, Units, or Other Rights that Have Not Vested ($)
5. Numeric values: return as plain digit strings (e.g. "1250000", "25.10").
   Remove currency symbols ($), commas, and whitespace. Use null for
   missing/dash/em-dash values.
6. Dates: keep as source text (e.g. "01/03/2022" or "2022-03-01").
7. Do NOT invent values. Return null where data is absent.
8. Skip header rows, separator rows, and footnote rows.
9. Ignore footnote superscripts (e.g. "(1)", "(5)") within cell values.
"""

# ── structured output JSON schema ──────────────────────────────
_RESPONSE_SCHEMA = {
    "type": "json_schema",
    "json_schema": {
        "name": "outstanding_equity_awards",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {
                "rows": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "grant_date": {"type": ["string", "null"]},
                            "options_exercisable": {"type": ["string", "null"]},
                            "options_unexercisable": {"type": ["string", "null"]},
                            "equity_incentive_unearned_options": {"type": ["string", "null"]},
                            "option_exercise_price": {"type": ["string", "null"]},
                            "option_expiration_date": {"type": ["string", "null"]},
                            "stock_unvested_shares": {"type": ["string", "null"]},
                            "stock_unvested_value": {"type": ["string", "null"]},
                            "equity_incentive_unearned_shares": {"type": ["string", "null"]},
                            "equity_incentive_unearned_value": {"type": ["string", "null"]},
                        },
                        "required": [
                            "name",
                            "grant_date",
                            "options_exercisable",
                            "options_unexercisable",
                            "equity_incentive_unearned_options",
                            "option_exercise_price",
                            "option_expiration_date",
                            "stock_unvested_shares",
                            "stock_unvested_value",
                            "equity_incentive_unearned_shares",
                            "equity_incentive_unearned_value",
                        ],
                        "additionalProperties": False,
                    },
                },
                "confidence": {"type": "number"},
                "notes": {"type": ["string", "null"]},
            },
            "required": ["rows", "confidence", "notes"],
            "additionalProperties": False,
        },
    },
}


# ── extraction logic ───────────────────────────────────────────


def _extract_raw_html_table(raw_html: str, table_block: TableBlock) -> str:
    """Extract the raw HTML table from filing source using block char offsets.

    Falls back to linearized_text when:
    - char offsets are degenerate (start == end, common when the parser
      can't locate the serialized tag in raw HTML)
    - the extracted snippet exceeds MAX_RAW_HTML_BYTES
    """
    start = table_block.source_char_start
    end = table_block.source_char_end
    if start >= 0 and end > start and end <= len(raw_html):
        snippet = raw_html[start:end]
        if len(snippet.encode("utf-8", errors="replace")) <= MAX_RAW_HTML_BYTES:
            return snippet
        log.info("Raw HTML table too large (%d bytes), using linearized_text", len(snippet.encode("utf-8", errors="replace")))
    else:
        log.debug("Invalid char offsets (start=%d end=%d html_len=%d), using linearized_text", start, end, len(raw_html))
    return table_block.linearized_text


def _build_user_message(
    company_name: str,
    cik: str,
    filing_date: str,
    table_content: str,
) -> str:
    return (
        f"Company: {company_name}\n"
        f"CIK: {cik}\n"
        f"Filing date: {filing_date}\n\n"
        f"Outstanding Equity Awards at Fiscal Year-End Table:\n"
        f"{table_content}"
    )


def _call_llm(
    client: OpenAI,
    model: str,
    company_name: str,
    cik: str,
    filing_date: str,
    table_content: str,
) -> CompanyOutstandingEquityAwardsResult:
    """Call OpenAI with structured outputs and return validated result."""
    user_message = _build_user_message(company_name, cik, filing_date, table_content)

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        response_format=_RESPONSE_SCHEMA,
        max_completion_tokens=8192,
    )

    raw_text = response.choices[0].message.content or "{}"
    data = json.loads(raw_text)
    # The structured output schema allows null for notes, but Pydantic model expects str
    if data.get("notes") is None:
        data["notes"] = ""
    return CompanyOutstandingEquityAwardsResult.model_validate(data)


def _row_to_csv(
    record: OutstandingEquityAwardRecord,
    cik: str,
    company_name: str,
    filing_url: str,
) -> dict[str, str]:
    """Map an extraction record to the output CSV column schema."""
    return {
        "CIK": cik,
        "Company Name": company_name,
        "Filing URL": filing_url,
        "Name": record.name,
        "Grant Date": record.grant_date or "",
        "Option Award (Number of Securities Underlying Unexercised Options Exercisable (#))": record.options_exercisable or "",
        "Option Award (Number of Securities Underlying Unexercised Options Unexercisable (#))": record.options_unexercisable or "",
        "Option Award (Equity Incentive Plan Awards: Number of Securities Underlying Unexercised Unearned Options (#))": record.equity_incentive_unearned_options or "",
        "Option Exercise Price ($)": record.option_exercise_price or "",
        "Option Expiration Date": record.option_expiration_date or "",
        "Stock Awards (Number of Shares or Units of Stock that Have Not Vested (#))": record.stock_unvested_shares or "",
        "Stock Awards (Market Value of Shares or Units of Stock that Have Not Vested ($))": record.stock_unvested_value or "",
        "Stock Awards (Equity Incentive Plan Awards: Number of Unearned Shares, Units, or Other Rights that Have Not Vested (#))": record.equity_incentive_unearned_shares or "",
        "Stock Awards (Equity Incentive Plan Awards: Market or Payout Value of Unearned Shares, Units, or Other Rights that Have Not Vested ($))": record.equity_incentive_unearned_value or "",
    }


# ── CIK processing ────────────────────────────────────────────


def process_cik(
    cik: str,
    client: OpenAI,
    model: str,
) -> list[dict[str, str]]:
    """Process one CIK end-to-end and return CSV rows."""
    filing = fetch_latest_def14a(cik)
    company_name = str(getattr(filing, "company_name", "") or "")
    filing_url = str(getattr(filing, "filing_url", "") or "")
    filing_date_str = str(getattr(filing, "filing_date", "") or "")

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

    table_block, _ = _locate_outstanding_equity_awards_table(blocks)
    if table_block is None:
        log.warning("No Outstanding Equity Awards table found | cik=%s", cik)
        return []

    table_content = _extract_raw_html_table(filing.raw_html, table_block)

    result = _call_llm(
        client=client,
        model=model,
        company_name=company_name,
        cik=cik,
        filing_date=filing_date_str,
        table_content=table_content,
    )

    return [
        _row_to_csv(record, cik, company_name, filing_url)
        for record in result.rows
    ]


# ── CLI entry point ────────────────────────────────────────────


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Standalone Outstanding Equity Awards extractor for DEF 14A filings.",
    )
    parser.add_argument(
        "--input",
        required=True,
        help="CSV file with 'cik' column",
    )
    parser.add_argument(
        "--output",
        default="output/outstanding_equity_awards.csv",
        help="Output CSV path (default: output/outstanding_equity_awards.csv)",
    )
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help=f"OpenAI model (default: {DEFAULT_MODEL})",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Max CIKs to process",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s | %(message)s",
        stream=sys.stderr,
    )

    # Load .env for OPENAI_API_KEY
    load_dotenv(_PROJECT_ROOT / ".env", override=False)

    # Read CIK list
    input_path = Path(args.input)
    if not input_path.exists():
        log.error("Input file not found: %s", input_path)
        sys.exit(1)

    with input_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        cik_rows = list(reader)
        fieldnames = reader.fieldnames or []

    cik_col = next(
        (col for col in fieldnames if col.lower() in {"cik", "folder_id"}),
        fieldnames[0] if fieldnames else "cik",
    )
    ciks = [row[cik_col].strip() for row in cik_rows if row.get(cik_col, "").strip()]
    if args.limit:
        ciks = ciks[: args.limit]

    log.info("Starting extraction | ciks=%d model=%s output=%s", len(ciks), args.model, args.output)

    # Initialize OpenAI client
    client = OpenAI()

    # Prepare output
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    total_rows = 0
    tables_found = 0
    tables_missing = 0
    errors = 0

    with output_path.open("w", newline="", encoding="utf-8") as out_file:
        writer = csv.DictWriter(out_file, fieldnames=OUTPUT_COLUMNS)
        writer.writeheader()

        for i, cik in enumerate(ciks, 1):
            try:
                rows = process_cik(cik, client, args.model)
                if rows:
                    tables_found += 1
                    for row in rows:
                        writer.writerow(row)
                    total_rows += len(rows)
                    log.info("[%d/%d] CIK=%s ... %d rows extracted", i, len(ciks), cik, len(rows))
                else:
                    tables_missing += 1
                    log.info("[%d/%d] CIK=%s ... no table found", i, len(ciks), cik)
            except Exception as exc:  # noqa: BLE001
                errors += 1
                log.error("[%d/%d] CIK=%s ... error: %s", i, len(ciks), cik, exc)

        out_file.flush()

    log.info(
        "Done | processed=%d tables_found=%d no_table=%d errors=%d total_rows=%d output=%s",
        len(ciks),
        tables_found,
        tables_missing,
        errors,
        total_rows,
        output_path,
    )


if __name__ == "__main__":
    main()
