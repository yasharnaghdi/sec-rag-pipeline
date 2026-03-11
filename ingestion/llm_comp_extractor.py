"""
LLM-based compensation extractor for SEC DEF 14A proxy filings.

Purpose
-------
This module is the FALLBACK extraction layer. It is called by the
batch pipeline only when the deterministic comp_table_extractor
returns zero rows for a given company. It must never be called
as the primary extractor -- deterministic extraction is cheaper,
faster, and fully auditable.

Design constraints
------------------
1. Input is ALWAYS TableBlock.linearized_text (a single compact
   string) -- never raw HTML, never the full filing. This keeps
   token usage bounded (typically 300-800 tokens per table) and
   extraction focused on the correct source block.

2. Output is a strict Pydantic model (CompanyCompResult) validated
   before returning. If the LLM response fails JSON parsing or
   schema validation, one retry is attempted with an explicit
   correction prompt. After two failures, a CompanyCompResult with
   all role fields empty and confidence=0.0 is returned.

3. Role assignment rules (applied by the LLM via prompt, and
   re-validated in Python post-parse):
   - CEO: title contains "Chief Executive Officer" or "CEO"
   - CFO: title contains "Chief Financial Officer" or "CFO"
   - COO: title contains "Chief Operating Officer" or "COO"
   - President: title contains "President" (if no CEO match)
   - Up to 2 remaining named executives -> other1, other2
   - If multiple candidates per role, select highest total comp

4. Numeric values must be returned as plain digit strings without
   currency symbols (e.g. "1250000") or null. The caller
   (run_batch50_key_results.py) applies clean_numeric() for final
   float conversion.

5. The model name is configurable but defaults to "gpt-4o-mini"
   which balances accuracy and cost for structured table extraction.

6. This module has zero side effects: no file I/O, no DB writes,
   no logging of raw LLM responses (to avoid secret leakage).
"""
from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, cast

from dotenv import load_dotenv
from openai import OpenAI
from pydantic import BaseModel, Field, model_validator

log = logging.getLogger(__name__)


class ExecCompRecord(BaseModel):
    """Compensation record for one named executive officer."""

    name: str = Field(default="", description="Full name of the executive")
    title: str = Field(default="", description="Official title as stated in the filing")
    salary: str | None = Field(
        default=None,
        description=(
            "Base salary for the most recent fiscal year as a plain digit string "
            "(e.g. '1250000'). No currency symbols, no commas. Null if not found."
        ),
    )
    bonus: str | None = Field(
        default=None,
        description="Cash bonus as plain digit string or null.",
    )
    stock_awards: str | None = Field(
        default=None,
        description="Stock awards value as plain digit string or null.",
    )
    option_awards: str | None = Field(
        default=None,
        description="Option awards value as plain digit string or null.",
    )
    non_equity_incentive: str | None = Field(
        default=None,
        description="Non-equity incentive plan compensation as plain digit string or null.",
    )
    pension_change: str | None = Field(
        default=None,
        description=(
            "Change in pension value and nonqualified deferred compensation earnings "
            "as plain digit string or null."
        ),
    )
    other_comp: str | None = Field(
        default=None,
        description="All other compensation as plain digit string or null.",
    )
    total: str | None = Field(
        default=None,
        description=(
            "Total compensation for the most recent fiscal year as a plain digit "
            "string (e.g. '1800000'). No currency symbols. Null if not found."
        ),
    )
    footnotes: str | None = Field(
        default=None,
        description="Footnote references/text for the row, or null if unavailable.",
    )
    fiscal_year: str = Field(
        default="",
        description="Fiscal year of the compensation values (e.g. '2023').",
    )

    @model_validator(mode="after")
    def normalize_fields(self) -> ExecCompRecord:
        """Normalize numeric and year fields for robust downstream mapping."""
        numeric_fields = (
            "salary",
            "bonus",
            "stock_awards",
            "option_awards",
            "non_equity_incentive",
            "pension_change",
            "other_comp",
            "total",
        )
        for field_name in numeric_fields:
            raw_value = cast(str | None, getattr(self, field_name))
            setattr(self, field_name, _normalize_numeric_value(raw_value))

        self.name = self.name.strip()
        self.title = self.title.strip()
        self.footnotes = _normalize_optional_text(self.footnotes)
        self.fiscal_year = _normalize_fiscal_year(self.fiscal_year)
        return self


def _exec_comp_record_has_payload(record: ExecCompRecord | None) -> bool:
    """Return True when an executive compensation record has any payload."""
    if record is None:
        return False
    values = [
        record.name.strip(),
        record.title.strip(),
        str(record.salary or "").strip(),
        str(record.bonus or "").strip(),
        str(record.stock_awards or "").strip(),
        str(record.option_awards or "").strip(),
        str(record.non_equity_incentive or "").strip(),
        str(record.pension_change or "").strip(),
        str(record.other_comp or "").strip(),
        str(record.total or "").strip(),
        str(record.footnotes or "").strip(),
        str(record.fiscal_year or "").strip(),
    ]
    return any(values)


def _normalize_optional_text(value: str | None) -> str | None:
    text = str(value or "").strip()
    return text if text else None


def _normalize_fiscal_year(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    match = re.search(r"\b(19|20)\d{2}\b", text)
    if match:
        return match.group(0)
    return ""


def _normalize_numeric_value(value: str | None) -> str | None:
    """Normalize raw numeric table text to plain digit strings or null."""
    text = str(value or "").strip()
    if not text:
        return None

    lowered = text.lower()
    if lowered in {"-", "—", "–", "n/a", "na", "none", "null"}:
        return None

    # Standalone footnote references like "(4)" should not become amounts.
    if re.fullmatch(r"\(?\d{1,3}\)?", text) and "$" not in text and "," not in text and "." not in text:
        return None

    candidates: list[str] = re.findall(r"\d[\d,]*(?:\.\d+)?", text)
    if not candidates:
        return None

    best: str = max(candidates, key=lambda token: len(re.sub(r"\D", "", token)))
    normalized: str = best.replace(",", "")
    if not re.fullmatch(r"\d+(?:\.\d+)?", normalized):
        return None

    try:
        if float(normalized) == 0.0:
            return None
    except ValueError:
        return None
    return normalized


class CompanyCompResult(BaseModel):
    """
    Row-level compensation result for one company filing.

    Primary output is row-based and mirrors master compensation CSV columns
    (excluding filing/company metadata added by the pipeline):
      Name, Title, Year, Salary, Bonus, Stock Awards, Option Awards,
      Non-Equity Incentive, Pension Change, All Other Compensation, Total,
      Extra information (footnotes).

    Role fields are retained for backward compatibility with legacy callers.
    """

    rows: list[ExecCompRecord] = Field(
        default_factory=list,
        description="Row-level compensation records aligned to master compensation CSV output columns.",
    )
    ceo: ExecCompRecord | None = Field(default_factory=ExecCompRecord)
    cfo: ExecCompRecord | None = Field(default_factory=ExecCompRecord)
    coo: ExecCompRecord | None = Field(default_factory=ExecCompRecord)
    other1: ExecCompRecord | None = Field(default_factory=ExecCompRecord)
    other2: ExecCompRecord | None = Field(default_factory=ExecCompRecord)
    confidence: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Self-assessed extraction confidence 0.0-1.0.",
    )
    notes: str = Field(
        default="",
        description="Brief extraction notes or caveats from the LLM.",
    )

    @model_validator(mode="after")
    def normalize_result(self) -> CompanyCompResult:
        """Normalize confidence and role/rows compatibility after validation."""
        self.confidence = max(0.0, min(1.0, self.confidence))

        # Accept role=null outputs from LLM by normalizing to empty records.
        for role_key in ("ceo", "cfo", "coo", "other1", "other2"):
            if getattr(self, role_key) is None:
                setattr(self, role_key, ExecCompRecord())

        # Backfill rows from legacy role-shaped payloads when rows are absent.
        if not self.rows:
            role_records = [self.ceo, self.cfo, self.coo, self.other1, self.other2]
            self.rows = [
                record
                for record in role_records
                if isinstance(record, ExecCompRecord) and _exec_comp_record_has_payload(record)
            ]

        return self


class GrantPlanAwardRecord(BaseModel):
    """One row from Grants of Plan-Based Awards."""

    name: str = Field(default="", description="Name column value as shown in filing.")
    grant_type: str = Field(default="", description="Grant type text as stated in filing.")
    grant_date: str | None = Field(default=None, description="Grant date text.")
    non_equity_threshold: str | None = Field(default=None, description="Non-equity threshold amount.")
    non_equity_target: str | None = Field(default=None, description="Non-equity target amount.")
    non_equity_maximum: str | None = Field(default=None, description="Non-equity maximum amount.")
    equity_threshold: str | None = Field(default=None, description="Equity threshold amount.")
    equity_target: str | None = Field(default=None, description="Equity target amount.")
    equity_maximum: str | None = Field(default=None, description="Equity maximum amount.")
    all_other_stock_awards_shares: str | None = Field(
        default=None,
        description="All other stock awards number of shares/units.",
    )
    all_other_option_awards_securities: str | None = Field(
        default=None,
        description="All other option awards number of securities underlying options.",
    )
    exercise_or_base_price: str | None = Field(
        default=None,
        description="Exercise or base price of option awards.",
    )
    grant_date_fair_value: str | None = Field(
        default=None,
        description="Grant date fair value of stock and option awards.",
    )

class CompanyGrantsResult(BaseModel):
    """Structured grants extraction result for one filing."""

    rows: list[GrantPlanAwardRecord] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    notes: str = Field(default="")

    @model_validator(mode="after")
    def clamp_confidence(self) -> CompanyGrantsResult:
        self.confidence = max(0.0, min(1.0, self.confidence))
        return self


class OutstandingEquityAwardRecord(BaseModel):
    """One row from Outstanding Equity Awards at Fiscal Year-End."""

    name: str = Field(default="", description="Name column value as shown in filing.")
    grant_date: str | None = Field(default=None, description="Grant date text.")
    options_exercisable: str | None = Field(
        default=None,
        description="Number of securities underlying unexercised options exercisable.",
    )
    options_unexercisable: str | None = Field(
        default=None,
        description="Number of securities underlying unexercised options unexercisable.",
    )
    equity_incentive_unearned_options: str | None = Field(
        default=None,
        description="Equity incentive plan awards number of securities underlying unexercised unearned options.",
    )
    option_exercise_price: str | None = Field(default=None, description="Option exercise price.")
    option_expiration_date: str | None = Field(default=None, description="Option expiration date text.")
    stock_unvested_shares: str | None = Field(
        default=None,
        description="Number of shares or units of stock that have not vested.",
    )
    stock_unvested_value: str | None = Field(
        default=None,
        description="Market value of shares or units of stock that have not vested.",
    )
    equity_incentive_unearned_shares: str | None = Field(
        default=None,
        description="Equity incentive plan awards number of unearned shares/units/rights not vested.",
    )
    equity_incentive_unearned_value: str | None = Field(
        default=None,
        description="Equity incentive plan awards market or payout value of unearned shares/units/rights not vested.",
    )

    @model_validator(mode="after")
    def normalize_fields(self) -> OutstandingEquityAwardRecord:
        numeric_fields = (
            "options_exercisable",
            "options_unexercisable",
            "equity_incentive_unearned_options",
            "option_exercise_price",
            "stock_unvested_shares",
            "stock_unvested_value",
            "equity_incentive_unearned_shares",
            "equity_incentive_unearned_value",
        )
        for field_name in numeric_fields:
            raw_value = cast(str | None, getattr(self, field_name))
            setattr(self, field_name, _normalize_numeric_value(raw_value))

        self.name = self.name.strip()
        self.grant_date = _normalize_optional_text(self.grant_date)
        self.option_expiration_date = _normalize_optional_text(self.option_expiration_date)
        return self


class CompanyOutstandingEquityAwardsResult(BaseModel):
    """Structured outstanding-equity-awards extraction result for one filing."""

    rows: list[OutstandingEquityAwardRecord] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    notes: str = Field(default="")

    @model_validator(mode="after")
    def clamp_confidence(self) -> CompanyOutstandingEquityAwardsResult:
        self.confidence = max(0.0, min(1.0, self.confidence))
        return self


_SYSTEM_PROMPT = """\
You are a financial data extraction assistant specialised in SEC DEF 14A
proxy statement compensation tables.

You will receive the linearized text of a Summary Compensation Table from
a proxy statement. Extract row-level compensation data and return ONLY a
valid JSON object matching the schema below.

EXTRACTION RULES:
1. If a target fiscal year is provided in the user message, extract values
   for that fiscal year only. Otherwise use the most recent fiscal year
   present in the table.
2. Output row schema must match master compensation CSV semantics:
   - rows[].name -> Name
   - rows[].title -> Title
   - rows[].fiscal_year -> Year
   - rows[].salary -> Salary ($)
   - rows[].bonus -> Bonus Awards ($)
   - rows[].stock_awards -> Stock Awards ($)
   - rows[].option_awards -> Option Awards ($)
   - rows[].non_equity_incentive -> Non-Equity Incentive Plan Compensation ($)
   - rows[].pension_change -> Change in pension value and nonqualified deferred compensation earnings ($)
   - rows[].other_comp -> All Other Compensation ($)
   - rows[].total -> Total ($)
   - rows[].footnotes -> Extra information
3. Field-to-column mapping is strict. Extract each JSON field from the exact
   Summary Compensation Table column with the same meaning:
   - salary -> "Salary ($)"
   - bonus -> "Bonus Awards ($)" (bonus column only)
   - stock_awards -> "Stock Awards ($)"
   - option_awards -> "Option Awards ($)"
   - non_equity_incentive -> "Non-Equity Incentive Plan Compensation ($)"
   - pension_change -> "Change in pension value and nonqualified deferred compensation earnings ($)"
   - other_comp -> "All Other Compensation ($)" only
   - total -> "Total ($)"
   - footnotes -> row-specific footnote refs/text only ("Extra information")
4. NEVER swap columns:
   - Do not put non-equity incentive amounts into bonus.
   - Do not put bonus amounts into non_equity_incentive.
   - Do not put stock awards into option_awards.
   - Do not put option awards into stock_awards.
   - Do not copy total into any component field.
   - Do not set other_comp from total.
   - Do not calculate other_comp as a residual (e.g. total minus other fields).
   - If the "All Other Compensation" cell is blank/missing/dash, set other_comp = null.
   - other_comp can equal total only when the source row explicitly shows the same
     number in both "All Other Compensation" and "Total" columns.
5. salary, bonus, stock_awards, option_awards, non_equity_incentive,
   pension_change, other_comp, total should preserve the raw numeric table
   value in string form (currency symbols/commas allowed) or null.
   Examples: "$ 1,250,000", "1,250,000", "1250000", null.
   Use null if the value is missing, zero, a dash, or the table cell is blank.
5b. If a column does not exist in the table header, set the corresponding
    JSON field to null for every row. Never move values into a different field.
5c. If a column exists but the row cell is blank/dash/placeholder, return null
    for that field in that row.
6. fiscal_year should be a 4-digit year string (e.g. "2023") and must come
   from the table row/year header, not from filing metadata.
7. confidence: 0.0 (cannot extract reliably) to 1.0 (clean extraction).
8. Do NOT invent values. If data is absent, use empty string or null.
9. Keep top-level JSON keys exactly: rows, confidence, notes.
10. rows must be a JSON array (possibly empty) and each element must be an
    object with keys:
    name, title, salary, bonus, stock_awards, option_awards,
    non_equity_incentive, pension_change, other_comp, total,
    footnotes, fiscal_year
11. Return ONLY the JSON object. No prose, no markdown code fences.
12. Do NOT infer fiscal year from filing date. Use explicit year values from
   the table rows.
"""

_RETRY_SYSTEM_PROMPT = """\
Your previous response was not valid JSON or did not match the required
schema. Return ONLY the corrected JSON object using the schema provided.
Do not include any explanation, markdown, or code fences.
Required top-level keys: rows, confidence, notes.
Do NOT output role keys (ceo/cfo/coo/other1/other2) in retry.
Output compact JSON only (no trailing commas, no comments, no extra keys).
"""


def _extract_comp_column_availability(table_text: str) -> dict[str, bool] | None:
    """Infer which compensation columns are present from row-style header text."""
    header_line: str | None = None
    for raw_line in table_text.splitlines()[:16]:
        line = raw_line.strip()
        if not line or "|" not in line:
            continue
        if not line.lower().startswith("row "):
            continue
        _, _, body = line.partition(":")
        body_text = body.strip()
        lowered = body_text.lower()
        if "year" in lowered and ("salary" in lowered or "total" in lowered):
            header_line = body_text
            break

    if not header_line:
        return None

    header_cells = [cell.strip().lower() for cell in header_line.split("|") if cell.strip()]
    if not header_cells:
        return None

    def has_any(*needles: str) -> bool:
        return any(any(needle in cell for needle in needles) for cell in header_cells)

    return {
        "salary": has_any("salary"),
        "bonus": has_any("bonus"),
        "stock_awards": has_any("stock award"),
        "option_awards": has_any("option award", "option/ sar award", "option/sar award"),
        "non_equity_incentive": has_any("non-equity incentive", "non equity incentive"),
        "pension_change": has_any("pension value", "deferred compensation"),
        "other_comp": has_any("all other compensation"),
        "total": has_any("total"),
    }


def _iter_comp_records(result: CompanyCompResult) -> list[ExecCompRecord]:
    """Collect row and legacy role records from an extraction result."""
    records: list[ExecCompRecord] = []
    records.extend(record for record in result.rows if isinstance(record, ExecCompRecord))
    for role_key in ("ceo", "cfo", "coo", "other1", "other2"):
        role_record = getattr(result, role_key, None)
        if isinstance(role_record, ExecCompRecord):
            records.append(role_record)
    return records


def _collect_comp_column_violations(
    result: CompanyCompResult,
    column_availability: dict[str, bool] | None,
) -> list[str]:
    """Detect mismatches between extracted payload and available table columns."""
    if not column_availability:
        return []

    violations: list[str] = []
    fields = (
        "salary",
        "bonus",
        "stock_awards",
        "option_awards",
        "non_equity_incentive",
        "pension_change",
        "other_comp",
        "total",
    )

    for record in _iter_comp_records(result):
        for field_name in fields:
            if column_availability.get(field_name, True):
                continue
            if getattr(record, field_name):
                violations.append(f"missing_column_has_value:{field_name}")

        if (
            column_availability.get("bonus", False)
            and not column_availability.get("stock_awards", False)
            and not record.bonus
            and bool(record.stock_awards)
        ):
            violations.append("bonus_value_mapped_to_stock_awards")

    return violations


def _apply_comp_column_guardrails(
    result: CompanyCompResult,
    column_availability: dict[str, bool] | None,
) -> bool:
    """Coerce extracted values to respect available columns."""
    if not column_availability:
        return False

    changed = False
    fields = (
        "salary",
        "bonus",
        "stock_awards",
        "option_awards",
        "non_equity_incentive",
        "pension_change",
        "other_comp",
        "total",
    )

    for record in _iter_comp_records(result):
        if (
            column_availability.get("bonus", False)
            and not column_availability.get("stock_awards", False)
            and not record.bonus
            and bool(record.stock_awards)
        ):
            record.bonus = record.stock_awards
            record.stock_awards = None
            changed = True

        for field_name in fields:
            if column_availability.get(field_name, True):
                continue
            if getattr(record, field_name) is not None:
                setattr(record, field_name, None)
                changed = True

    return changed


def _build_comp_guardrail_retry_prompt(
    column_availability: dict[str, bool] | None,
    violations: list[str],
) -> str:
    """Build retry prompt for field/header mismatches."""
    if not column_availability:
        return _RETRY_SYSTEM_PROMPT

    field_labels = {
        "salary": "Salary ($)",
        "bonus": "Bonus Awards ($)",
        "stock_awards": "Stock Awards ($)",
        "option_awards": "Option Awards ($)",
        "non_equity_incentive": "Non-Equity Incentive Plan Compensation ($)",
        "pension_change": "Change in pension value and nonqualified deferred compensation earnings ($)",
        "other_comp": "All Other Compensation ($)",
        "total": "Total ($)",
    }
    present_columns = [label for key, label in field_labels.items() if column_availability.get(key, False)]
    missing_columns = [label for key, label in field_labels.items() if not column_availability.get(key, False)]
    violation_text = ", ".join(violations[:10]) if violations else "unknown"

    return (
        "Your previous JSON has column-mapping violations and must be corrected.\n"
        f"Present columns: {present_columns}\n"
        f"Missing columns: {missing_columns}\n"
        f"Violations: {violation_text}\n"
        "Rules:\n"
        "1) If a column is missing from header, set that field to null in every row.\n"
        "2) If Bonus exists and Stock Awards is missing, place values in bonus and set stock_awards=null.\n"
        "3) Return ONLY compact valid JSON with keys rows, confidence, notes.\n"
        "Do NOT output role keys (ceo/cfo/coo/other1/other2)."
    )


_GRANTS_SYSTEM_PROMPT = """\
You are a financial data extraction assistant specialised in SEC DEF 14A
proxy statement compensation tables.

You will receive the linearized text of a Grants of Plan-Based Awards table.
Extract each row and return ONLY valid JSON matching the schema below.

EXTRACTION RULES:
1. Preserve row granularity. If one person appears multiple times for different
   grant types, keep separate rows with the same name.
2. Keep non-equity and equity triplets semantically distinct:
   - non_equity_threshold/target/maximum come ONLY from non-equity incentive plan awards.
   - equity_threshold/target/maximum come ONLY from equity incentive plan awards.
3. grant_type should preserve the source row wording (do not normalize labels).
4. Numeric values must be plain digit strings where possible; use null for
   missing/dash values.
5. Do NOT invent values. Return empty strings or null where data is absent.
6. Return ONLY JSON. No prose and no markdown.

REQUIRED JSON SCHEMA:
{
  "rows": [
    {
      "name": "",
      "grant_type": "",
      "grant_date": null,
      "non_equity_threshold": null,
      "non_equity_target": null,
      "non_equity_maximum": null,
      "equity_threshold": null,
      "equity_target": null,
      "equity_maximum": null,
      "all_other_stock_awards_shares": null,
      "all_other_option_awards_securities": null,
      "exercise_or_base_price": null,
      "grant_date_fair_value": null
    }
  ],
  "confidence": 0.0,
  "notes": ""
}
"""

_OUTSTANDING_EQUITY_SYSTEM_PROMPT = """\
You are a financial data extraction assistant specialised in SEC DEF 14A
proxy statement compensation tables.

You will receive the linearized text of an Outstanding Equity Awards at Fiscal
Year-End table. Extract each row and return ONLY valid JSON matching the
schema below.

EXTRACTION RULES:
1. Preserve row granularity and keep one output row per source row.
2. Field mapping is strict:
   - options_exercisable <- Number of Securities Underlying Unexercised Options Exercisable (#)
   - options_unexercisable <- Number of Securities Underlying Unexercised Options Unexercisable (#)
   - equity_incentive_unearned_options <- Equity Incentive Plan Awards: Number of Securities Underlying Unexercised Unearned Options (#)
   - option_exercise_price <- Option Exercise Price ($)
   - option_expiration_date <- Option Expiration Date
   - stock_unvested_shares <- Number of Shares or Units of Stock that Have Not Vested (#)
   - stock_unvested_value <- Market Value of Shares or Units of Stock that Have Not Vested ($)
   - equity_incentive_unearned_shares <- Equity Incentive Plan Awards: Number of Unearned Shares, Units, or Other Rights that Have Not Vested (#)
   - equity_incentive_unearned_value <- Equity Incentive Plan Awards: Market or Payout Value of Unearned Shares, Units, or Other Rights that Have Not Vested ($)
3. Numeric values should be plain digit strings where possible. Use null for
   missing/dash values. Keep dates as source text.
4. Do NOT invent values. Return empty strings or null where data is absent.
5. Return ONLY JSON. No prose and no markdown.

REQUIRED JSON SCHEMA:
{
  "rows": [
    {
      "name": "",
      "grant_date": null,
      "options_exercisable": null,
      "options_unexercisable": null,
      "equity_incentive_unearned_options": null,
      "option_exercise_price": null,
      "option_expiration_date": null,
      "stock_unvested_shares": null,
      "stock_unvested_value": null,
      "equity_incentive_unearned_shares": null,
      "equity_incentive_unearned_value": null
    }
  ],
  "confidence": 0.0,
  "notes": ""
}
"""

# ── Ollama fallback configuration ────────────────────────────────
# When OPENAI_API_KEY is "dummy", empty, or an OpenAI auth error
# occurs, the extractor falls back to a local Ollama instance.
# The Ollama base URL and model are configurable via environment
# variables so Docker Compose can override them without code changes.
# OLLAMA_BASE_URL: full URL of the Ollama HTTP API
#   default: http://localhost:11434
#   docker compose override: http://ollama:11434
# OLLAMA_MODEL: model tag pulled into the Ollama instance
#   default: llama3.1 (8B, good balance of speed and accuracy)
#   alternatives: mistral, qwen2.5, phi3
# The Ollama prompt is identical to the OpenAI prompt so extraction
# quality is comparable for standard DEF 14A tables.
_OLLAMA_BASE_URL_DEFAULT = "http://localhost:11434"
_OLLAMA_MODEL_DEFAULT = "llama3.1"
_DUMMY_KEY_VALUES = {"dummy", "", "your-key-here", "sk-dummy"}


def _build_user_message(
    company_name: str,
    cik: str,
    filing_date: str,
    table_text: str,
    table_label: str = "Summary Compensation Table",
    target_fiscal_year: int | None = None,
) -> str:
    """Build the user turn message for the extraction prompt.

    Includes minimal filing metadata as context to help the LLM
    identify the correct fiscal year and company context.
    """
    target_year_line = (
        f"Target fiscal year: {target_fiscal_year} (extract this year only)\n"
        if target_fiscal_year is not None
        else "Target fiscal year: none (use most recent year in table)\n"
    )
    return (
        f"Company: {company_name}\n"
        f"CIK: {cik}\n"
        f"Filing date: {filing_date}\n\n"
        f"{target_year_line}\n"
        f"{table_label} (linearized):\n"
        f"{table_text}"
    )


def _call_openai(
    client: OpenAI,
    messages: list[dict[str, str]],
    model: str,
    max_tokens: int = 800,
) -> str:
    """Call OpenAI chat completions and return raw response text.

    Uses response_format={"type": "json_object"} to enforce JSON
    output at the API level (supported by gpt-4o-mini and gpt-4o).
    This reduces but does not eliminate the need for validation.

    Args:
        client: Initialised OpenAI client.
        messages: Chat history in OpenAI message format.
        model: Model identifier string.

    Returns:
        Raw string content from the first choice.

    Raises:
        openai.OpenAIError: On API errors (network, auth, rate limit).
    """
    # `type: ignore[arg-type]` is intentional because openai stubs and our
    # dict[str, str] messages shape do not perfectly align.
    response = client.chat.completions.create(  # type: ignore[call-overload]
        model=model,
        messages=messages,
        response_format={"type": "json_object"},
        temperature=0.0,
        max_tokens=max_tokens,
    )
    return response.choices[0].message.content or ""


def _call_ollama(
    messages: list[dict[str, str]],
    model: str | None = None,
    base_url: str | None = None,
) -> str:
    """Call a local Ollama instance and return raw response text.

    Uses the ollama Python client which communicates with the Ollama
    HTTP API. The model must already be pulled in the Ollama instance
    (handled by Docker Compose entrypoint or manual `ollama pull`).

    Prompt construction mirrors the OpenAI path exactly so extraction
    logic is model-agnostic. We request JSON output via the system
    prompt; Ollama does not have a native json_object response_format
    enforcer, so we rely on the system prompt instruction and the
    retry path in extract_company_comp_from_summary_table().

    Args:
        messages: Chat history in OpenAI-compatible message format.
                  Ollama's Python client accepts the same format.
        model: Ollama model tag. Defaults to OLLAMA_MODEL env var
               or _OLLAMA_MODEL_DEFAULT.
        base_url: Ollama API base URL. Defaults to OLLAMA_BASE_URL
                  env var or _OLLAMA_BASE_URL_DEFAULT.

    Returns:
        Raw string content from the Ollama response.

    Raises:
        Exception: Any connection or model error from the Ollama client.
                   Caller handles all exceptions.
    """
    import ollama

    resolved_model = model or os.environ.get("OLLAMA_MODEL", _OLLAMA_MODEL_DEFAULT)
    resolved_url = base_url or os.environ.get("OLLAMA_BASE_URL", _OLLAMA_BASE_URL_DEFAULT)

    # Ollama client accepts host as a constructor argument.
    client = ollama.Client(host=resolved_url)
    raw_response = client.chat(
        model=resolved_model,
        messages=messages,  # type: ignore[arg-type]
        options={"temperature": 0},
    )
    response = cast(dict[str, Any], raw_response)
    message = cast(dict[str, Any], response.get("message", {}))
    return str(message.get("content", ""))


def _is_openai_auth_error(exc: Exception) -> bool:
    """Return True when an OpenAI exception looks like an auth failure."""
    class_name = exc.__class__.__name__.lower()
    message = str(exc).lower()
    auth_markers = ("auth", "api key", "unauthorized", "invalid_api_key", "permission")
    return "authentication" in class_name or any(marker in message for marker in auth_markers)


def _parse_and_validate(raw: str) -> CompanyCompResult | None:
    """Parse raw LLM JSON string into a validated CompanyCompResult.

    Returns None if JSON is malformed or Pydantic validation fails.
    Caller is responsible for retry logic.
    """
    try:
        data: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.debug("LLM JSON parse error: %s", exc)
        return None
    try:
        return CompanyCompResult.model_validate(data)
    except Exception as exc:  # noqa: BLE001
        log.debug("LLM schema validation error: %s", exc)
        return None


def _parse_and_validate_grants(raw: str) -> CompanyGrantsResult | None:
    """Parse raw LLM JSON string into a validated CompanyGrantsResult."""
    try:
        data: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.debug("LLM JSON parse error (grants): %s", exc)
        return None
    try:
        return CompanyGrantsResult.model_validate(data)
    except Exception as exc:  # noqa: BLE001
        log.debug("LLM schema validation error (grants): %s", exc)
        return None


def _parse_and_validate_outstanding_equity_awards(
    raw: str,
) -> CompanyOutstandingEquityAwardsResult | None:
    """Parse raw LLM JSON into CompanyOutstandingEquityAwardsResult."""
    try:
        data: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError as exc:
        log.debug("LLM JSON parse error (outstanding equity): %s", exc)
        return None
    try:
        return CompanyOutstandingEquityAwardsResult.model_validate(data)
    except Exception as exc:  # noqa: BLE001
        log.debug("LLM schema validation error (outstanding equity): %s", exc)
        return None


def extract_company_comp_from_summary_table(
    *,
    company_name: str,
    cik: str,
    filing_date: str,
    accession_number: str,
    table_text: str,
    target_fiscal_year: int | None = None,
    model: str = "gpt-4o-mini",
    client: OpenAI | None = None,
) -> CompanyCompResult:
    """
    Extract role-keyed executive compensation from a linearized
    Summary Compensation Table using OpenAI.

    This is the FALLBACK path. Call this only when the deterministic
    comp_table_extractor.extract_summary_compensation() returns zero
    rows for a company.

    The function makes at most 2 API calls (attempt + 1 retry on
    invalid JSON/schema). If both fail, a CompanyCompResult with all
    empty fields and confidence=0.0 is returned -- never raises.

    Args:
        company_name: Human-readable company name for LLM context.
        cik: SEC CIK (zero-padded or bare) for provenance logging.
        filing_date: ISO date string of the filing (e.g. "2023-04-15").
        accession_number: Accession number for provenance logging only.
        table_text: TableBlock.linearized_text from the located Summary
                    Compensation Table. Must be non-empty.
        target_fiscal_year: Optional target fiscal year to extract from the
                            table (e.g. 2024). If None, extract most recent.
        model: OpenAI model identifier. Defaults to "gpt-4o-mini".
        client: Optional pre-initialised OpenAI client. If None, a new
                client is created using OPENAI_API_KEY from environment.

    Returns:
        CompanyCompResult with role-keyed extraction results.
        Always returns a result object; never raises.
    """
    if not table_text or not table_text.strip():
        log.warning(
            "llm_extractor | empty table_text | cik=%s accession=%s",
            cik,
            accession_number,
        )
        return CompanyCompResult()

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key.strip():
        # Best-effort load of project .env for script/CLI code paths
        # that do not go through Settings() initialization.
        project_root = Path(__file__).resolve().parents[1]
        load_dotenv(project_root / ".env", override=False)
        api_key = os.environ.get("OPENAI_API_KEY", "")
    use_ollama = client is None and api_key.strip().lower() in _DUMMY_KEY_VALUES
    if client is None and not use_ollama:
        client = OpenAI(api_key=api_key)

    user_message = _build_user_message(
        company_name,
        cik,
        filing_date,
        table_text,
        target_fiscal_year=target_fiscal_year,
    )

    messages: list[dict[str, str]] = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]

    # ── Route to Ollama if OpenAI key is absent/dummy ─────────────
    # This allows full local operation (Docker Compose, offline dev)
    # without requiring a paid OpenAI key. The Ollama path uses the
    # same prompts and validation logic as the OpenAI path.
    if use_ollama:
        log.info(
            "llm_extractor | routing to Ollama (no valid OpenAI key) | "
            "cik=%s model=%s",
            cik,
            os.environ.get("OLLAMA_MODEL", _OLLAMA_MODEL_DEFAULT),
        )
    else:
        log.info(
            "llm_extractor | attempt 1 via OpenAI | cik=%s model=%s "
            "tokens_approx=%d",
            cik,
            model,
            len(table_text.split()),
        )

    # Attempt 1
    try:
        if use_ollama:
            raw = _call_ollama(messages)
        else:
            if client is None:
                client = OpenAI(api_key=api_key)
            raw = _call_openai(client, messages, model, max_tokens=1800)
    except Exception as exc:  # noqa: BLE001
        if not use_ollama and _is_openai_auth_error(exc):
            log.warning(
                "llm_extractor | OpenAI auth failed, falling back to Ollama | "
                "cik=%s error=%s",
                cik,
                exc,
            )
            use_ollama = True
            try:
                raw = _call_ollama(messages)
            except Exception as ollama_exc:  # noqa: BLE001
                log.error(
                    "llm_extractor | API error attempt 1 | cik=%s backend=%s error=%s",
                    cik,
                    "ollama",
                    ollama_exc,
                )
                return CompanyCompResult()
        else:
            log.error(
                "llm_extractor | API error attempt 1 | cik=%s backend=%s error=%s",
                cik,
                "ollama" if use_ollama else "openai",
                exc,
            )
            return CompanyCompResult()

    column_availability = _extract_comp_column_availability(table_text)
    guardrail_fallback_result: CompanyCompResult | None = None
    retry_prompt = _RETRY_SYSTEM_PROMPT

    result = _parse_and_validate(raw)
    if result is not None:
        guardrail_violations = _collect_comp_column_violations(result, column_availability)
        if not guardrail_violations:
            log.info(
                "llm_extractor | success attempt 1 | cik=%s confidence=%.2f",
                cik,
                result.confidence,
            )
            return result
        guardrail_fallback_result = result
        retry_prompt = _build_comp_guardrail_retry_prompt(column_availability, guardrail_violations)
        log.warning(
            "llm_extractor | guardrail violation attempt 1 | cik=%s violations=%s",
            cik,
            ",".join(guardrail_violations[:6]),
        )
    else:
        log.warning("llm_extractor | invalid response attempt 1 | cik=%s", cik)

    retry_messages = messages + [
        {"role": "assistant", "content": raw},
        {"role": "user", "content": retry_prompt},
    ]

    log.info("llm_extractor | attempt 2 (retry) | cik=%s", cik)

    try:
        if use_ollama:
            raw_retry = _call_ollama(retry_messages)
        else:
            if client is None:
                client = OpenAI(api_key=api_key)
            raw_retry = _call_openai(client, retry_messages, model, max_tokens=2200)
    except Exception as exc:  # noqa: BLE001
        if not use_ollama and _is_openai_auth_error(exc):
            log.warning(
                "llm_extractor | OpenAI auth failed on retry, using Ollama | "
                "cik=%s error=%s",
                cik,
                exc,
            )
            use_ollama = True
            try:
                raw_retry = _call_ollama(retry_messages)
            except Exception as ollama_exc:  # noqa: BLE001
                log.error(
                    "llm_extractor | API error attempt 2 | cik=%s backend=%s error=%s",
                    cik,
                    "ollama",
                    ollama_exc,
                )
                if guardrail_fallback_result is not None:
                    _apply_comp_column_guardrails(guardrail_fallback_result, column_availability)
                    return guardrail_fallback_result
                return CompanyCompResult()
        else:
            log.error(
                "llm_extractor | API error attempt 2 | cik=%s backend=%s error=%s",
                cik,
                "ollama" if use_ollama else "openai",
                exc,
            )
            if guardrail_fallback_result is not None:
                _apply_comp_column_guardrails(guardrail_fallback_result, column_availability)
                return guardrail_fallback_result
            return CompanyCompResult()

    result_retry = _parse_and_validate(raw_retry)
    if result_retry is not None:
        retry_violations = _collect_comp_column_violations(result_retry, column_availability)
        if retry_violations:
            _apply_comp_column_guardrails(result_retry, column_availability)
            log.warning(
                "llm_extractor | guardrail coercion after retry | cik=%s violations=%s",
                cik,
                ",".join(retry_violations[:6]),
            )
        log.info(
            "llm_extractor | success attempt 2 | cik=%s confidence=%.2f",
            cik,
            result_retry.confidence,
        )
        return result_retry

    if guardrail_fallback_result is not None:
        _apply_comp_column_guardrails(guardrail_fallback_result, column_availability)
        log.warning("llm_extractor | using attempt 1 with local guardrails | cik=%s", cik)
        return guardrail_fallback_result

    log.error(
        "llm_extractor | failed both attempts | cik=%s accession=%s",
        cik,
        accession_number,
    )
    return CompanyCompResult()


def extract_grants_from_plan_based_table(
    *,
    company_name: str,
    cik: str,
    filing_date: str,
    accession_number: str,
    table_text: str,
    model: str = "gpt-4o-mini",
    client: OpenAI | None = None,
) -> CompanyGrantsResult:
    """Extract row-level Grants of Plan-Based Awards data from one table."""
    if not table_text or not table_text.strip():
        log.warning(
            "llm_extractor_grants | empty table_text | cik=%s accession=%s",
            cik,
            accession_number,
        )
        return CompanyGrantsResult()

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key.strip():
        project_root = Path(__file__).resolve().parents[1]
        load_dotenv(project_root / ".env", override=False)
        api_key = os.environ.get("OPENAI_API_KEY", "")
    use_ollama = client is None and api_key.strip().lower() in _DUMMY_KEY_VALUES
    if client is None and not use_ollama:
        client = OpenAI(api_key=api_key)

    user_message = _build_user_message(
        company_name,
        cik,
        filing_date,
        table_text,
        table_label="Grants of Plan-Based Awards Table",
    )
    messages: list[dict[str, str]] = [
        {"role": "system", "content": _GRANTS_SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]

    if use_ollama:
        log.info(
            "llm_extractor_grants | routing to Ollama (no valid OpenAI key) | "
            "cik=%s model=%s",
            cik,
            os.environ.get("OLLAMA_MODEL", _OLLAMA_MODEL_DEFAULT),
        )
    else:
        log.info(
            "llm_extractor_grants | attempt 1 via OpenAI | cik=%s model=%s tokens_approx=%d",
            cik,
            model,
            len(table_text.split()),
        )

    try:
        if use_ollama:
            raw = _call_ollama(messages)
        else:
            if client is None:
                client = OpenAI(api_key=api_key)
            raw = _call_openai(client, messages, model)
    except Exception as exc:  # noqa: BLE001
        if not use_ollama and _is_openai_auth_error(exc):
            log.warning(
                "llm_extractor_grants | OpenAI auth failed, falling back to Ollama | "
                "cik=%s error=%s",
                cik,
                exc,
            )
            use_ollama = True
            try:
                raw = _call_ollama(messages)
            except Exception as ollama_exc:  # noqa: BLE001
                log.error(
                    "llm_extractor_grants | API error attempt 1 | cik=%s backend=%s error=%s",
                    cik,
                    "ollama",
                    ollama_exc,
                )
                return CompanyGrantsResult()
        else:
            log.error(
                "llm_extractor_grants | API error attempt 1 | cik=%s backend=%s error=%s",
                cik,
                "ollama" if use_ollama else "openai",
                exc,
            )
            return CompanyGrantsResult()

    result = _parse_and_validate_grants(raw)
    if result is not None:
        log.info(
            "llm_extractor_grants | success attempt 1 | cik=%s confidence=%.2f rows=%d",
            cik,
            result.confidence,
            len(result.rows),
        )
        return result

    log.warning("llm_extractor_grants | invalid response attempt 1 | cik=%s", cik)
    retry_messages = messages + [
        {"role": "assistant", "content": raw},
        {"role": "user", "content": _RETRY_SYSTEM_PROMPT},
    ]

    log.info("llm_extractor_grants | attempt 2 (retry) | cik=%s", cik)
    try:
        if use_ollama:
            raw_retry = _call_ollama(retry_messages)
        else:
            if client is None:
                client = OpenAI(api_key=api_key)
            raw_retry = _call_openai(client, retry_messages, model)
    except Exception as exc:  # noqa: BLE001
        if not use_ollama and _is_openai_auth_error(exc):
            log.warning(
                "llm_extractor_grants | OpenAI auth failed on retry, using Ollama | "
                "cik=%s error=%s",
                cik,
                exc,
            )
            use_ollama = True
            try:
                raw_retry = _call_ollama(retry_messages)
            except Exception as ollama_exc:  # noqa: BLE001
                log.error(
                    "llm_extractor_grants | API error attempt 2 | cik=%s backend=%s error=%s",
                    cik,
                    "ollama",
                    ollama_exc,
                )
                return CompanyGrantsResult()
        else:
            log.error(
                "llm_extractor_grants | API error attempt 2 | cik=%s backend=%s error=%s",
                cik,
                "ollama" if use_ollama else "openai",
                exc,
            )
            return CompanyGrantsResult()

    result_retry = _parse_and_validate_grants(raw_retry)
    if result_retry is not None:
        log.info(
            "llm_extractor_grants | success attempt 2 | cik=%s confidence=%.2f rows=%d",
            cik,
            result_retry.confidence,
            len(result_retry.rows),
        )
        return result_retry

    log.error(
        "llm_extractor_grants | failed both attempts | cik=%s accession=%s",
        cik,
        accession_number,
    )
    return CompanyGrantsResult()


def extract_outstanding_equity_awards_table(
    *,
    company_name: str,
    cik: str,
    filing_date: str,
    accession_number: str,
    table_text: str,
    model: str = "gpt-4o-mini",
    client: OpenAI | None = None,
) -> CompanyOutstandingEquityAwardsResult:
    """Extract row-level Outstanding Equity Awards at Fiscal Year-End data from one table."""
    if not table_text or not table_text.strip():
        log.warning(
            "llm_extractor_outstanding_equity | empty table_text | cik=%s accession=%s",
            cik,
            accession_number,
        )
        return CompanyOutstandingEquityAwardsResult()

    api_key = os.environ.get("OPENAI_API_KEY", "")
    if not api_key.strip():
        project_root = Path(__file__).resolve().parents[1]
        load_dotenv(project_root / ".env", override=False)
        api_key = os.environ.get("OPENAI_API_KEY", "")
    use_ollama = client is None and api_key.strip().lower() in _DUMMY_KEY_VALUES
    if client is None and not use_ollama:
        client = OpenAI(api_key=api_key)

    user_message = _build_user_message(
        company_name,
        cik,
        filing_date,
        table_text,
        table_label="Outstanding Equity Awards at Fiscal Year-End Table",
    )
    messages: list[dict[str, str]] = [
        {"role": "system", "content": _OUTSTANDING_EQUITY_SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]

    if use_ollama:
        log.info(
            "llm_extractor_outstanding_equity | routing to Ollama (no valid OpenAI key) | "
            "cik=%s model=%s",
            cik,
            os.environ.get("OLLAMA_MODEL", _OLLAMA_MODEL_DEFAULT),
        )
    else:
        log.info(
            "llm_extractor_outstanding_equity | attempt 1 via OpenAI | cik=%s model=%s tokens_approx=%d",
            cik,
            model,
            len(table_text.split()),
        )

    try:
        if use_ollama:
            raw = _call_ollama(messages)
        else:
            if client is None:
                client = OpenAI(api_key=api_key)
            raw = _call_openai(client, messages, model)
    except Exception as exc:  # noqa: BLE001
        if not use_ollama and _is_openai_auth_error(exc):
            log.warning(
                "llm_extractor_outstanding_equity | OpenAI auth failed, falling back to Ollama | "
                "cik=%s error=%s",
                cik,
                exc,
            )
            use_ollama = True
            try:
                raw = _call_ollama(messages)
            except Exception as ollama_exc:  # noqa: BLE001
                log.error(
                    "llm_extractor_outstanding_equity | API error attempt 1 | cik=%s backend=%s error=%s",
                    cik,
                    "ollama",
                    ollama_exc,
                )
                return CompanyOutstandingEquityAwardsResult()
        else:
            log.error(
                "llm_extractor_outstanding_equity | API error attempt 1 | cik=%s backend=%s error=%s",
                cik,
                "ollama" if use_ollama else "openai",
                exc,
            )
            return CompanyOutstandingEquityAwardsResult()

    result = _parse_and_validate_outstanding_equity_awards(raw)
    if result is not None:
        log.info(
            "llm_extractor_outstanding_equity | success attempt 1 | cik=%s confidence=%.2f rows=%d",
            cik,
            result.confidence,
            len(result.rows),
        )
        return result

    log.warning("llm_extractor_outstanding_equity | invalid response attempt 1 | cik=%s", cik)
    retry_messages = messages + [
        {"role": "assistant", "content": raw},
        {"role": "user", "content": _RETRY_SYSTEM_PROMPT},
    ]

    log.info("llm_extractor_outstanding_equity | attempt 2 (retry) | cik=%s", cik)
    try:
        if use_ollama:
            raw_retry = _call_ollama(retry_messages)
        else:
            if client is None:
                client = OpenAI(api_key=api_key)
            raw_retry = _call_openai(client, retry_messages, model)
    except Exception as exc:  # noqa: BLE001
        if not use_ollama and _is_openai_auth_error(exc):
            log.warning(
                "llm_extractor_outstanding_equity | OpenAI auth failed on retry, using Ollama | "
                "cik=%s error=%s",
                cik,
                exc,
            )
            use_ollama = True
            try:
                raw_retry = _call_ollama(retry_messages)
            except Exception as ollama_exc:  # noqa: BLE001
                log.error(
                    "llm_extractor_outstanding_equity | API error attempt 2 | cik=%s backend=%s error=%s",
                    cik,
                    "ollama",
                    ollama_exc,
                )
                return CompanyOutstandingEquityAwardsResult()
        else:
            log.error(
                "llm_extractor_outstanding_equity | API error attempt 2 | cik=%s backend=%s error=%s",
                cik,
                "ollama" if use_ollama else "openai",
                exc,
            )
            return CompanyOutstandingEquityAwardsResult()

    result_retry = _parse_and_validate_outstanding_equity_awards(raw_retry)
    if result_retry is not None:
        log.info(
            "llm_extractor_outstanding_equity | success attempt 2 | cik=%s confidence=%.2f rows=%d",
            cik,
            result_retry.confidence,
            len(result_retry.rows),
        )
        return result_retry

    log.error(
        "llm_extractor_outstanding_equity | failed both attempts | cik=%s accession=%s",
        cik,
        accession_number,
    )
    return CompanyOutstandingEquityAwardsResult()
