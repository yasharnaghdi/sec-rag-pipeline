"""LLM extraction of role-aware compensation fields from Summary Compensation tables."""
from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from datetime import date

from dotenv import load_dotenv
from openai import OpenAI

log = logging.getLogger(__name__)

_MAX_OTHERS = 2
_MAX_RETRIES = 2


@dataclass(frozen=True)
class _RoleComp:
    name: str
    title: str
    salary: str | None
    total: str | None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "name": self.name,
            "title": self.title,
            "salary": self.salary,
            "total": self.total,
        }


def _coerce_optional_text(value: object) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _coerce_optional_numeric_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.lower() in {"none", "null", "n/a", "na", "unknown"}:
        return None
    return text


def _coerce_role(payload: object) -> _RoleComp:
    if not isinstance(payload, dict):
        return _RoleComp(name="", title="", salary=None, total=None)
    return _RoleComp(
        name=_coerce_optional_text(payload.get("name")),
        title=_coerce_optional_text(payload.get("title")),
        salary=_coerce_optional_numeric_text(payload.get("salary")),
        total=_coerce_optional_numeric_text(payload.get("total")),
    )


def _validate_comp_payload(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ValueError("LLM payload must be a JSON object")

    ceo = _coerce_role(payload.get("ceo"))
    cfo = _coerce_role(payload.get("cfo"))
    coo = _coerce_role(payload.get("coo"))

    raw_others = payload.get("others", [])
    if not isinstance(raw_others, list):
        raise ValueError("'others' must be a list")
    if len(raw_others) > _MAX_OTHERS:
        raise ValueError("'others' exceeds maximum length of 2")
    others = [_coerce_role(entry).as_dict() for entry in raw_others]

    confidence_raw = payload.get("confidence")
    if isinstance(confidence_raw, (int, float, str)):
        try:
            confidence = float(confidence_raw)
        except ValueError as exc:
            raise ValueError("confidence must be numeric") from exc
    else:
        raise ValueError("confidence must be numeric")
    if confidence < 0.0 or confidence > 1.0:
        raise ValueError("confidence must be between 0 and 1")

    notes = _coerce_optional_text(payload.get("notes"))
    return {
        "ceo": ceo.as_dict(),
        "cfo": cfo.as_dict(),
        "coo": coo.as_dict(),
        "others": others,
        "confidence": confidence,
        "notes": notes,
    }


def _response_text_to_json(response_text: str) -> dict[str, object]:
    text = response_text.strip()
    if text.startswith("```"):
        lines = [line for line in text.splitlines() if not line.strip().startswith("```")]
        text = "\n".join(lines).strip()
    parsed = json.loads(text)
    return _validate_comp_payload(parsed)


def _build_messages(
    *,
    company_name: str,
    cik: str,
    filing_date: str,
    accession_number: str,
    table_text: str,
    previous_error: str | None,
) -> list[dict[str, str]]:
    system_msg = (
        "You extract executive compensation fields from SEC Summary Compensation tables.\n"
        "Return valid JSON only, no prose.\n"
        "Use only the provided table text.\n"
        "Find Salary and Total values for the most recent year in the table.\n"
        "Role mapping rules:\n"
        "- CEO: title contains 'Chief Executive Officer' or 'CEO'.\n"
        "- CFO: title contains 'Chief Financial Officer' or 'CFO'.\n"
        "- COO: title contains 'Chief Operating Officer' or 'COO'.\n"
        "- If multiple candidates for a role, choose the one with highest total.\n"
        "- If a role is not found, keep empty strings for name/title and null for salary/total.\n"
        "Output schema exactly:\n"
        "{\n"
        '  "ceo": {"name": string, "title": string, "salary": string|null, "total": string|null},\n'
        '  "cfo": {"name": string, "title": string, "salary": string|null, "total": string|null},\n'
        '  "coo": {"name": string, "title": string, "salary": string|null, "total": string|null},\n'
        '  "others": [\n'
        '    {"name": string, "title": string, "salary": string|null, "total": string|null}\n'
        "  ],\n"
        '  "confidence": number,\n'
        '  "notes": string\n'
        "}\n"
        "Constraints:\n"
        "- others must contain at most 2 records.\n"
        "- confidence must be between 0 and 1.\n"
        "- salary/total values must be numeric strings without $ or commas, or null.\n"
    )
    user_msg = (
        f"Company: {company_name}\n"
        f"CIK: {cik}\n"
        f"Filing date: {filing_date}\n"
        f"Accession number: {accession_number}\n"
        f"Summary compensation table text:\n{table_text}"
    )
    if previous_error:
        user_msg += f"\n\nPrevious output failed validation: {previous_error}"
    return [
        {"role": "system", "content": system_msg},
        {"role": "user", "content": user_msg},
    ]


def extract_company_comp_from_summary_table(
    *,
    company_name: str,
    cik: str,
    filing_date: str,
    accession_number: str,
    table_text: str,
    model: str = "gpt-4o-mini",
) -> dict[str, object]:
    """Extract role-aligned compensation values from summary compensation table text."""
    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY environment variable is required")

    parsed_date = date.fromisoformat(filing_date[:10]) if filing_date else None
    filing_date_iso = parsed_date.isoformat() if parsed_date is not None else filing_date

    client = OpenAI(api_key=api_key)
    last_error: Exception | None = None
    previous_error: str | None = None

    for attempt in range(_MAX_RETRIES):
        messages = _build_messages(
            company_name=company_name,
            cik=cik,
            filing_date=filing_date_iso,
            accession_number=accession_number,
            table_text=table_text,
            previous_error=previous_error,
        )
        response = client.chat.completions.create(
            model=model,
            temperature=0.0,
            response_format={"type": "json_object"},
            messages=messages,
        )  # type: ignore[call-overload]
        content = response.choices[0].message.content
        if content is None:
            last_error = ValueError("OpenAI response content is empty")
            previous_error = str(last_error)
            continue
        try:
            return _response_text_to_json(content)
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            last_error = exc
            previous_error = str(exc)
            log.warning(
                "LLM extractor validation failed on attempt %s/%s for CIK %s accession %s: %s",
                attempt + 1,
                _MAX_RETRIES,
                cik,
                accession_number,
                exc,
            )

    if last_error is None:
        last_error = RuntimeError("LLM extraction failed for unknown reason")
    raise ValueError(f"Failed to parse valid compensation JSON after {_MAX_RETRIES} attempts: {last_error}")
