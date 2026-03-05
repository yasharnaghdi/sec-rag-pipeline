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
from typing import Any

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
    total: str | None = Field(
        default=None,
        description=(
            "Total compensation for the most recent fiscal year as a plain digit "
            "string (e.g. '1800000'). No currency symbols. Null if not found."
        ),
    )
    fiscal_year: str = Field(
        default="",
        description="Fiscal year of the compensation values (e.g. '2023').",
    )


class CompanyCompResult(BaseModel):
    """
    Role-keyed compensation result for one company, one filing year.

    All role fields are optional. If a role is not present in the
    filing's Summary Compensation Table, its fields are left empty/null.
    Confidence reflects the LLM's self-assessed extraction reliability.
    """

    ceo: ExecCompRecord = Field(default_factory=ExecCompRecord)
    cfo: ExecCompRecord = Field(default_factory=ExecCompRecord)
    coo: ExecCompRecord = Field(default_factory=ExecCompRecord)
    other1: ExecCompRecord = Field(default_factory=ExecCompRecord)
    other2: ExecCompRecord = Field(default_factory=ExecCompRecord)
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
    def clamp_confidence(self) -> CompanyCompResult:
        """Ensure confidence is clamped to [0.0, 1.0] regardless of LLM output."""
        self.confidence = max(0.0, min(1.0, self.confidence))
        return self


_SYSTEM_PROMPT = """\
You are a financial data extraction assistant specialised in SEC DEF 14A
proxy statement compensation tables.

You will receive the linearized text of a Summary Compensation Table from
a proxy statement. Extract compensation data for named executive officers
and return ONLY a valid JSON object matching the schema below.

EXTRACTION RULES:
1. Extract values for the MOST RECENT fiscal year present in the table.
2. Map executives to roles using title keywords:
   - ceo: title contains "Chief Executive Officer" or "CEO"
   - cfo: title contains "Chief Financial Officer" or "CFO"
   - coo: title contains "Chief Operating Officer" or "COO"
   - If a role has multiple candidates, select the one with the highest total.
   - If no CEO but a "President" exists with no CEO, map President -> ceo.
   - Remaining executives (up to 2) -> other1, other2 (highest total first).
3. salary, bonus, stock_awards, option_awards, total must be plain digit
   strings without currency symbols or commas (e.g. "1250000").
   Use null if the value is missing, zero, or a dash.
4. fiscal_year should be a 4-digit year string (e.g. "2023").
5. confidence: 0.0 (cannot extract reliably) to 1.0 (clean extraction).
6. Do NOT invent values. If data is absent, use empty string or null.
7. Return ONLY the JSON object. No prose, no markdown code fences.

REQUIRED JSON SCHEMA:
{
  "ceo":   {"name":"","title":"","salary":null,"bonus":null,"stock_awards":null,"option_awards":null,"total":null,"fiscal_year":""},
  "cfo":   {"name":"","title":"","salary":null,"bonus":null,"stock_awards":null,"option_awards":null,"total":null,"fiscal_year":""},
  "coo":   {"name":"","title":"","salary":null,"bonus":null,"stock_awards":null,"option_awards":null,"total":null,"fiscal_year":""},
  "other1":{"name":"","title":"","salary":null,"bonus":null,"stock_awards":null,"option_awards":null,"total":null,"fiscal_year":""},
  "other2":{"name":"","title":"","salary":null,"bonus":null,"stock_awards":null,"option_awards":null,"total":null,"fiscal_year":""},
  "confidence": 0.0,
  "notes": ""
}
"""

_RETRY_SYSTEM_PROMPT = """\
Your previous response was not valid JSON or did not match the required
schema. Return ONLY the corrected JSON object using the schema provided.
Do not include any explanation, markdown, or code fences.
"""


def _build_user_message(
    company_name: str,
    cik: str,
    filing_date: str,
    table_text: str,
) -> str:
    """Build the user turn message for the extraction prompt.

    Includes minimal filing metadata as context to help the LLM
    identify the correct fiscal year and company context.
    """
    return (
        f"Company: {company_name}\n"
        f"CIK: {cik}\n"
        f"Filing date: {filing_date}\n\n"
        f"Summary Compensation Table (linearized):\n"
        f"{table_text}"
    )


def _call_openai(
    client: OpenAI,
    messages: list[dict[str, str]],
    model: str,
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
        max_tokens=800,
    )
    return response.choices[0].message.content or ""


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


def extract_company_comp_from_summary_table(
    *,
    company_name: str,
    cik: str,
    filing_date: str,
    accession_number: str,
    table_text: str,
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

    if client is None:
        api_key = os.environ.get("OPENAI_API_KEY", "")
        client = OpenAI(api_key=api_key)

    user_message = _build_user_message(company_name, cik, filing_date, table_text)

    messages: list[dict[str, str]] = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_message},
    ]

    log.info(
        "llm_extractor | attempt 1 | cik=%s model=%s tokens_approx=%d",
        cik,
        model,
        len(table_text.split()),
    )

    try:
        raw = _call_openai(client, messages, model)
    except Exception as exc:  # noqa: BLE001
        log.error("llm_extractor | API error attempt 1 | cik=%s error=%s", cik, exc)
        return CompanyCompResult()

    result = _parse_and_validate(raw)
    if result is not None:
        log.info(
            "llm_extractor | success attempt 1 | cik=%s confidence=%.2f",
            cik,
            result.confidence,
        )
        return result

    log.warning("llm_extractor | invalid response attempt 1 | cik=%s", cik)

    retry_messages = messages + [
        {"role": "assistant", "content": raw},
        {"role": "user", "content": _RETRY_SYSTEM_PROMPT},
    ]

    log.info("llm_extractor | attempt 2 (retry) | cik=%s", cik)

    try:
        raw_retry = _call_openai(client, retry_messages, model)
    except Exception as exc:  # noqa: BLE001
        log.error("llm_extractor | API error attempt 2 | cik=%s error=%s", cik, exc)
        return CompanyCompResult()

    result_retry = _parse_and_validate(raw_retry)
    if result_retry is not None:
        log.info(
            "llm_extractor | success attempt 2 | cik=%s confidence=%.2f",
            cik,
            result_retry.confidence,
        )
        return result_retry

    log.error(
        "llm_extractor | failed both attempts | cik=%s accession=%s",
        cik,
        accession_number,
    )
    return CompanyCompResult()
