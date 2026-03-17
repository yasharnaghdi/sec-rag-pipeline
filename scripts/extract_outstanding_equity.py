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
import time
from datetime import date
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup
from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, Field, field_validator, model_validator

# ── project imports ─────────────────────────────────────────────
# Ensure project root is on sys.path so ingestion imports resolve.
_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from ingestion.edgar_folder_fetcher import fetch_def14a_for_fiscal_year, fetch_latest_def14a  # noqa: E402
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
MAX_RAW_HTML_BYTES = 120_000  # fall back to linearized_text above this (post-minification)

BATCH_LOG_COLUMNS = [
    "cik",
    "company_name",
    "target_fiscal_year",
    "source_filing_date",
    "source_accession_number",
    "source_filing_url",
    "status",
    "rows_extracted",
    "llm_confidence",
    "llm_notes",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "elapsed_seconds",
    "error",
]

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

You will receive an Outstanding Equity Awards at Fiscal Year-End table
as HTML markup. Extract each data row and return ONLY valid JSON matching
the schema enforced by the response_format.

EXTRACTION RULES:

1. COLUMN IDENTIFICATION (HTML):
   Use the <tr> and <td>/<th> structure to identify columns. Pay close
   attention to colspan and rowspan attributes for merged cells and
   multi-level headers. The standard column order is:
     Name | Grant Date | Options Exercisable | Options Unexercisable |
     EIP Unearned Options | Exercise Price | Expiration Date |
     Stock Unvested Shares | Stock Unvested Value |
     EIP Unearned Shares | EIP Unearned Value
   Some tables omit certain columns — map based on header text, not position.

2. ONE OUTPUT ROW PER SOURCE DATA ROW:
   Each <tr> that contains numeric data (option counts, share counts,
   dollar values, or a grant date) MUST produce its own output row.
   An executive with 4 grant rows must yield 4 output rows. NEVER
   collapse multiple grants for the same person into a single row.

3. NAME PROPAGATION:
   An executive's name typically appears once and spans multiple rows
   (via rowspan or appearing only in the first row for that executive).
   For subsequent data rows with no name, carry forward the most recently
   seen executive name. If a row contains only a job title (e.g.
   "Chief Executive Officer", "Executive Chairman"), it is NOT a name —
   skip it or merge with the name from the prior row.

4. NAME CLEANING:
   Return ONLY the person's name. Strip any job title, position, or
   honorific suffix that appears on a separate line or after a line
   break in the same cell. Examples:
     "Stephen A. Remondi Chief Executive Officer and President" → "Stephen A. Remondi"
     "Dale Hooks Chief Commercial Officer" → "Dale Hooks"
   If the name is split across two <tr> rows (e.g. "Shoshana" in one
   row, "Shendelman, Ph.D." in the next), concatenate them.

5. FIELD MAPPING (strict):
   - name ← Name column
   - grant_date ← Grant Date
   - options_exercisable ← Number of Securities Underlying Unexercised Options Exercisable (#)
   - options_unexercisable ← Number of Securities Underlying Unexercised Options Unexercisable (#)
   - equity_incentive_unearned_options ← Equity Incentive Plan Awards: Number of Securities Underlying Unexercised Unearned Options (#)
   - option_exercise_price ← Option Exercise Price ($)
   - option_expiration_date ← Option Expiration Date
   - stock_unvested_shares ← Number of Shares or Units of Stock that Have Not Vested (#)
   - stock_unvested_value ← Market Value of Shares or Units of Stock that Have Not Vested ($)
   - equity_incentive_unearned_shares ← Equity Incentive Plan Awards: Number of Unearned Shares, Units, or Other Rights that Have Not Vested (#)
   - equity_incentive_unearned_value ← Equity Incentive Plan Awards: Market or Payout Value of Unearned Shares, Units, or Other Rights that Have Not Vested ($)

6. NUMERIC VALUES: return as plain digit strings (e.g. "1250000", "25.10").
   Remove currency symbols ($), commas, and whitespace. Use null for
   missing / dash / em-dash / "—" values.

7. DATES: keep as source text (e.g. "01/03/2022" or "2022-03-01").

8. Do NOT invent values. Return null where data is absent.

9. SKIP rows that are entirely empty (all values are null/dash).
   Also skip header rows, separator rows, and footnote rows.

10. Ignore footnote superscripts (e.g. "(1)", "(5)") within cell values.
    Cells that contain ONLY a footnote marker and no numeric data should
    be treated as null.

11. COLUMN DISAMBIGUATION: A data value belongs to one and only one column.
    Do NOT copy the same value into multiple output fields. If a source
    row has values in only the option columns (exercisable, unexercisable,
    price, expiration), leave the stock award fields as null, and vice versa.
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
    """Extract the raw HTML table from filing source.

    Strategy:
    1. Try char offsets from the parser (fast path).
    2. If offsets are degenerate (start == end — common bug in SECHTMLParser),
       fall back to a BeautifulSoup search that matches the table by content
       fingerprint (first data cells).
    3. If the HTML snippet exceeds MAX_RAW_HTML_BYTES, fall back to
       linearized_text.
    """
    # ── 1. Try char offsets ───────────────────────────────────────
    start = table_block.source_char_start
    end = table_block.source_char_end
    if start >= 0 and end > start and end <= len(raw_html):
        snippet = _minify_html_table(raw_html[start:end])
        byte_len = len(snippet.encode("utf-8", errors="replace"))
        if byte_len <= MAX_RAW_HTML_BYTES:
            log.debug("Using char-offset HTML (start=%d end=%d, minified=%d bytes)", start, end, byte_len)
            return snippet
        log.info(
            "Raw HTML table too large after minification (%d bytes), trying BeautifulSoup fallback",
            byte_len,
        )
    else:
        log.debug(
            "Invalid char offsets (start=%d end=%d html_len=%d), trying BeautifulSoup fallback",
            start, end, len(raw_html),
        )

    # ── 2. BeautifulSoup fallback ─────────────────────────────────
    html_snippet = _find_table_html_via_bs4(raw_html, table_block)
    if html_snippet is not None:
        return html_snippet

    # ── 3. Last resort: linearized text ───────────────────────────
    log.debug("BeautifulSoup fallback failed, using linearized_text")
    return table_block.linearized_text


# Keywords that must appear in the equity awards table (at least 3 of these).
_BS4_EQUITY_REQUIRED_KEYWORDS = [
    "grant date",
    "exercisable",
    "unexercisable",
    "exercise price",
    "expiration date",
    "unexercised",
    "have not vested",
    "option awards",
    "stock awards",
    "equity incentive plan",
]


def _minify_html_table(html: str) -> str:
    """Strip noise from SEC EDGAR HTML to reduce token count.

    Removes: style, class, bgcolor, width, height, align, valign, nowrap,
    cellspacing, cellpadding, border, id attributes. Unwraps <font> and
    <span> tags (keeps their text content). Collapses whitespace.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Strip attributes from all tags
    strip_attrs = {
        "style", "class", "bgcolor", "width", "height", "align",
        "valign", "nowrap", "cellspacing", "cellpadding", "border",
        "id", "size",
    }
    for tag in soup.find_all(True):
        for attr in list(tag.attrs.keys()):
            if attr in strip_attrs:
                del tag[attr]

    # Unwrap <font> and <span> tags (preserve their children)
    for tag_name in ("font", "span", "b", "i", "em", "strong", "sup", "u"):
        for tag in soup.find_all(tag_name):
            tag.unwrap()

    # Remove <br> tags
    for br in soup.find_all("br"):
        br.replace_with(" ")

    # Remove HTML comments
    from bs4 import Comment
    for comment in soup.find_all(string=lambda text: isinstance(text, Comment)):
        comment.extract()

    result = str(soup)
    # Collapse multiple whitespace
    result = re.sub(r"\s+", " ", result)
    # Collapse empty td/th tags
    result = re.sub(r"<td>\s*</td>", "<td></td>", result)
    result = re.sub(r"<th>\s*</th>", "<th></th>", result)
    return result.strip()


def _find_table_html_via_bs4(raw_html: str, table_block: TableBlock) -> str | None:
    """Find the matching <table> in raw HTML by fingerprinting data cells.

    Strategy:
    1. Build fingerprints from the parsed TableBlock's data cells.
    2. Iterate over all <table> elements — each candidate must contain
       equity award keywords (exercisable, grant date, etc.) to avoid
       matching Summary Compensation or Director Compensation tables.
    3. Score by fingerprint hits and pick the best match.
    4. Minify the HTML to reduce token count.
    """
    # Build fingerprints from the parsed rows — use non-empty data cells
    # that look like names or numbers (skip short footnote markers).
    fingerprints: list[str] = []
    for row in table_block.rows:
        for cell in row:
            cell_stripped = cell.strip()
            if not cell_stripped or len(cell_stripped) < 3:
                continue
            # Skip pure footnote markers like "(1)", "(2)"
            if re.fullmatch(r"\(\d+\)", cell_stripped):
                continue
            fingerprints.append(cell_stripped)
            if len(fingerprints) >= 20:
                break
        if len(fingerprints) >= 20:
            break

    if len(fingerprints) < 3:
        return None

    soup = BeautifulSoup(raw_html, "html.parser")
    best_table = None
    best_score = 0

    for table_tag in soup.find_all("table"):
        table_text = table_tag.get_text(" ", strip=True)
        table_text_lower = table_text.lower()

        # Gate: table must contain at least 3 equity-specific keywords
        keyword_hits = sum(
            1 for kw in _BS4_EQUITY_REQUIRED_KEYWORDS
            if kw in table_text_lower
        )
        if keyword_hits < 3:
            continue

        # Score by fingerprint match
        fp_score = sum(1 for fp in fingerprints if fp in table_text)
        # Bonus for more keyword hits
        combined_score = fp_score + keyword_hits
        if combined_score > best_score:
            best_score = combined_score
            best_table = table_tag

    if best_table is None or best_score < len(fingerprints) * 0.3:
        log.debug("BS4 fallback: no equity-keyword-qualified table matched (best_score=%d)", best_score)
        return None

    # Minify to reduce token count
    html_str = _minify_html_table(str(best_table))
    byte_len = len(html_str.encode("utf-8", errors="replace"))

    if byte_len > MAX_RAW_HTML_BYTES:
        log.info(
            "BeautifulSoup-found table still too large after minification (%d bytes > %d), using linearized_text",
            byte_len, MAX_RAW_HTML_BYTES,
        )
        return None

    log.info(
        "Using BeautifulSoup-extracted HTML table (%d bytes, keywords=%d, fp_score=%d/%d)",
        byte_len, sum(1 for kw in _BS4_EQUITY_REQUIRED_KEYWORDS if kw in best_table.get_text(" ", strip=True).lower()),
        best_score - sum(1 for kw in _BS4_EQUITY_REQUIRED_KEYWORDS if kw in best_table.get_text(" ", strip=True).lower()),
        len(fingerprints),
    )
    return html_str


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
) -> tuple[CompanyOutstandingEquityAwardsResult, dict[str, int]]:
    """Call OpenAI with structured outputs and return (result, token_usage).

    token_usage keys: prompt_tokens, completion_tokens, total_tokens
    """
    user_message = _build_user_message(company_name, cik, filing_date, table_content)

    prompt_chars = len(_SYSTEM_PROMPT) + len(user_message)
    log.debug(
        "LLM call | cik=%s prompt_chars=%d (system=%d user=%d)",
        cik, prompt_chars, len(_SYSTEM_PROMPT), len(user_message),
    )

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": user_message},
        ],
        response_format=_RESPONSE_SCHEMA,
        max_completion_tokens=16384,
    )

    usage = response.usage
    token_usage = {
        "prompt_tokens": usage.prompt_tokens if usage else 0,
        "completion_tokens": usage.completion_tokens if usage else 0,
        "total_tokens": usage.total_tokens if usage else 0,
    }
    log.info(
        "LLM tokens | cik=%s prompt=%d completion=%d total=%d finish_reason=%s",
        cik,
        token_usage["prompt_tokens"],
        token_usage["completion_tokens"],
        token_usage["total_tokens"],
        response.choices[0].finish_reason,
    )

    raw_text = response.choices[0].message.content or "{}"
    completion_chars = len(raw_text)
    log.debug("LLM response | cik=%s completion_chars=%d", cik, completion_chars)

    data = json.loads(raw_text)
    # The structured output schema allows null for notes, but Pydantic model expects str
    if data.get("notes") is None:
        data["notes"] = ""
    return CompanyOutstandingEquityAwardsResult.model_validate(data), token_usage


def _is_all_null_record(record: OutstandingEquityAwardRecord) -> bool:
    """Return True if every data field (excluding name) is null/empty."""
    data_fields = [
        record.grant_date,
        record.options_exercisable,
        record.options_unexercisable,
        record.equity_incentive_unearned_options,
        record.option_exercise_price,
        record.option_expiration_date,
        record.stock_unvested_shares,
        record.stock_unvested_value,
        record.equity_incentive_unearned_shares,
        record.equity_incentive_unearned_value,
    ]
    return all(not v for v in data_fields)


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
    filing_override: Any = None,
    target_fiscal_year: int | None = None,
) -> tuple[list[dict[str, str]], dict[str, Any]]:
    """Process one CIK end-to-end and return (csv_rows, log_row).

    If filing_override is provided, use it instead of fetching the latest filing.
    """
    t0 = time.monotonic()
    log_row: dict[str, Any] = {col: "" for col in BATCH_LOG_COLUMNS}
    log_row["cik"] = cik
    log_row["target_fiscal_year"] = str(target_fiscal_year) if target_fiscal_year else ""

    try:
        filing = filing_override if filing_override is not None else fetch_latest_def14a(cik)
        company_name = str(getattr(filing, "company_name", "") or "")
        filing_url = str(getattr(filing, "filing_url", "") or "")
        filing_date_str = str(getattr(filing, "filing_date", "") or "")

        log_row["company_name"] = company_name
        log_row["source_filing_date"] = filing_date_str
        log_row["source_accession_number"] = filing.accession_number
        log_row["source_filing_url"] = filing_url

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
            log_row["status"] = "no_table"
            log_row["rows_extracted"] = 0
            log_row["elapsed_seconds"] = f"{time.monotonic() - t0:.1f}"
            return [], log_row

        table_content = _extract_raw_html_table(filing.raw_html, table_block)

        result, token_usage = _call_llm(
            client=client,
            model=model,
            company_name=company_name,
            cik=cik,
            filing_date=filing_date_str,
            table_content=table_content,
        )

        rows = [
            _row_to_csv(record, cik, company_name, filing_url)
            for record in result.rows
            if not _is_all_null_record(record)
        ]

        log_row["status"] = "ok"
        log_row["rows_extracted"] = len(rows)
        log_row["llm_confidence"] = result.confidence
        log_row["llm_notes"] = result.notes or ""
        log_row["prompt_tokens"] = token_usage["prompt_tokens"]
        log_row["completion_tokens"] = token_usage["completion_tokens"]
        log_row["total_tokens"] = token_usage["total_tokens"]
        log_row["elapsed_seconds"] = f"{time.monotonic() - t0:.1f}"
        return rows, log_row

    except Exception as exc:
        log_row["status"] = "failed"
        log_row["rows_extracted"] = 0
        log_row["error"] = str(exc)
        log_row["elapsed_seconds"] = f"{time.monotonic() - t0:.1f}"
        raise


# ── CLI entry point ────────────────────────────────────────────


def _make_error_log_row(
    cik: str,
    target_fiscal_year: int | None,
    exc: Exception,
    *,
    status: str = "failed",
) -> dict[str, Any]:
    """Build a batch log row for an error case (when process_cik raised)."""
    row: dict[str, Any] = {col: "" for col in BATCH_LOG_COLUMNS}
    row["cik"] = cik
    row["target_fiscal_year"] = str(target_fiscal_year) if target_fiscal_year else ""
    row["status"] = status
    row["rows_extracted"] = 0
    row["error"] = str(exc)
    return row


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
        "--fiscal-year-start",
        type=int,
        default=None,
        help="Inclusive start fiscal year (must pair with --fiscal-year-end)",
    )
    parser.add_argument(
        "--fiscal-year-end",
        type=int,
        default=None,
        help="Inclusive end fiscal year (must pair with --fiscal-year-start)",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging",
    )
    args = parser.parse_args()

    has_start = args.fiscal_year_start is not None
    has_end = args.fiscal_year_end is not None
    if has_start != has_end:
        parser.error("Both --fiscal-year-start and --fiscal-year-end are required together.")
    if has_start and has_end:
        if args.fiscal_year_start < 1000 or args.fiscal_year_start > 9999:
            parser.error("--fiscal-year-start must be a 4-digit year.")
        if args.fiscal_year_end < 1000 or args.fiscal_year_end > 9999:
            parser.error("--fiscal-year-end must be a 4-digit year.")
        if args.fiscal_year_start > args.fiscal_year_end:
            parser.error("--fiscal-year-start cannot be greater than --fiscal-year-end.")

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

    fiscal_years: list[int] | None = None
    if args.fiscal_year_start is not None and args.fiscal_year_end is not None:
        fiscal_years = list(range(args.fiscal_year_start, args.fiscal_year_end + 1))

    year_label = f"{fiscal_years[0]}-{fiscal_years[-1]}" if fiscal_years else "latest"
    log.info(
        "Starting extraction | ciks=%d model=%s fiscal_years=%s output=%s",
        len(ciks), args.model, year_label, args.output,
    )

    # Initialize OpenAI client
    client = OpenAI()

    # Prepare output paths
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    batch_log_path = output_path.parent / f"{output_path.stem}_batch_log.csv"
    batch_log_failed_path = output_path.parent / f"{output_path.stem}_batch_log_failed.csv"

    total_rows = 0
    tables_found = 0
    tables_missing = 0
    errors = 0
    all_log_rows: list[dict[str, Any]] = []

    def _handle_result(
        rows: list[dict[str, str]],
        log_entry: dict[str, Any],
        writer: csv.DictWriter,
        log_writer: csv.DictWriter,
        label: str,
    ) -> None:
        nonlocal total_rows, tables_found, tables_missing
        all_log_rows.append(log_entry)
        log_writer.writerow(log_entry)
        if rows:
            tables_found += 1
            for row in rows:
                writer.writerow(row)
            total_rows += len(rows)
            log.info("%s ... %d rows extracted (confidence=%.2f)", label, len(rows), log_entry.get("llm_confidence", 0))
        else:
            tables_missing += 1
            log.info("%s ... no table found", label)

    def _handle_error(
        exc: Exception,
        log_entry: dict[str, Any],
        log_writer: csv.DictWriter,
        label: str,
        *,
        is_missing: bool = False,
    ) -> None:
        nonlocal errors, tables_missing
        all_log_rows.append(log_entry)
        log_writer.writerow(log_entry)
        if is_missing:
            tables_missing += 1
            log.warning("%s ... %s", label, exc)
        else:
            errors += 1
            log.error("%s ... error: %s", label, exc)

    with (
        output_path.open("w", newline="", encoding="utf-8") as out_file,
        batch_log_path.open("w", newline="", encoding="utf-8") as blog_file,
    ):
        writer = csv.DictWriter(out_file, fieldnames=OUTPUT_COLUMNS)
        log_writer = csv.DictWriter(blog_file, fieldnames=BATCH_LOG_COLUMNS)
        writer.writeheader()
        log_writer.writeheader()

        for i, cik in enumerate(ciks, 1):
            if fiscal_years is None:
                # Single latest filing per CIK
                label = f"[{i}/{len(ciks)}] CIK={cik}"
                try:
                    rows, log_entry = process_cik(cik, client, args.model)
                    _handle_result(rows, log_entry, writer, log_writer, label)
                except Exception as exc:  # noqa: BLE001
                    log_entry = _make_error_log_row(cik, None, exc)
                    _handle_error(exc, log_entry, log_writer, label)
            else:
                # Sweep across fiscal years
                for year in fiscal_years:
                    label = f"[{i}/{len(ciks)}] CIK={cik} year={year}"
                    try:
                        filing = fetch_def14a_for_fiscal_year(cik, year)
                        rows, log_entry = process_cik(
                            cik, client, args.model,
                            filing_override=filing,
                            target_fiscal_year=year,
                        )
                        _handle_result(rows, log_entry, writer, log_writer, label)
                    except ValueError as ve:
                        log_entry = _make_error_log_row(cik, year, ve, status="no_filing")
                        _handle_error(ve, log_entry, log_writer, label, is_missing=True)
                    except Exception as exc:  # noqa: BLE001
                        log_entry = _make_error_log_row(cik, year, exc)
                        _handle_error(exc, log_entry, log_writer, label)

        out_file.flush()
        blog_file.flush()

    # Write failed-only log
    failed_rows = [r for r in all_log_rows if r.get("status") not in ("ok",)]
    with batch_log_failed_path.open("w", newline="", encoding="utf-8") as failed_file:
        failed_writer = csv.DictWriter(failed_file, fieldnames=BATCH_LOG_COLUMNS)
        failed_writer.writeheader()
        for row in failed_rows:
            failed_writer.writerow(row)

    log.info(
        "Done | processed=%d tables_found=%d no_table=%d errors=%d total_rows=%d",
        len(ciks), tables_found, tables_missing, errors, total_rows,
    )
    log.info("output         -> %s", output_path)
    log.info("batch_log      -> %s", batch_log_path)
    log.info("batch_log_failed -> %s (rows=%d)", batch_log_failed_path, len(failed_rows))


if __name__ == "__main__":
    main()
