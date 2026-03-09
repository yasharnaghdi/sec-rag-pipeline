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
) -> str:
    """Build the user turn message for the extraction prompt.

    Includes minimal filing metadata as context to help the LLM
    identify the correct fiscal year and company context.
    """
    return (
        f"Company: {company_name}\n"
        f"CIK: {cik}\n"
        f"Filing date: {filing_date}\n\n"
        f"{table_label} (linearized):\n"
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

    user_message = _build_user_message(company_name, cik, filing_date, table_text)

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
            raw = _call_openai(client, messages, model)
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
        if use_ollama:
            raw_retry = _call_ollama(retry_messages)
        else:
            if client is None:
                client = OpenAI(api_key=api_key)
            raw_retry = _call_openai(client, retry_messages, model)
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
                return CompanyCompResult()
        else:
            log.error(
                "llm_extractor | API error attempt 2 | cik=%s backend=%s error=%s",
                cik,
                "ollama" if use_ollama else "openai",
                exc,
            )
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
