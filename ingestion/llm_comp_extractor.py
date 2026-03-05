"""LLM extraction of role-aware compensation fields from Summary Compensation tables."""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import date

from dotenv import load_dotenv
from openai import OpenAI

log = logging.getLogger(__name__)

_MAX_OTHERS = 2
_MAX_RETRIES = 2
_TSV_MARKER = "Compact TSV (first rows):"
_NUMERIC_RE = re.compile(r"^-?\d+(\.\d+)?$")
_FOOTNOTE_MARK_RE = re.compile(r"[\*\u2020\u2021]+$")


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


def _clean_numeric_text(value: str) -> str | None:
    text = value.strip()
    if not text:
        return None
    if text.lower() in {"none", "null", "n/a", "na", "unknown", "-", "—", "–"}:
        return None
    if text.startswith("(") and text.endswith(")"):
        text = f"-{text[1:-1]}"
    text = text.replace("$", "").replace(",", "").replace(" ", "")
    text = text.replace("—", "").replace("–", "")
    text = re.sub(r"[^0-9.\-]", "", text)
    if not text or text in {"-", ".", "-."}:
        return None
    if not _NUMERIC_RE.fullmatch(text):
        return None
    return text


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
    return _clean_numeric_text(text)


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


def _extract_rows_from_table_text(table_text: str) -> list[list[str]]:
    if _TSV_MARKER not in table_text:
        return []
    tsv_text = table_text.split(_TSV_MARKER, maxsplit=1)[1].strip()
    rows: list[list[str]] = []
    for line in tsv_text.splitlines():
        if not line.strip():
            continue
        cells = [cell.strip() for cell in line.split("\t")]
        if any(cells):
            rows.append(cells)
    return rows


def _find_table_indices(rows: list[list[str]]) -> tuple[int, int, int, int, int]:
    header_rows = rows[:12]
    name_index = 0
    title_index = -1
    salary_index = -1
    total_index = -1
    name_header_row = -1
    salary_header_row = -1
    total_header_row = -1

    for row_index, row in enumerate(header_rows):
        lowered = [cell.lower() for cell in row]
        if name_header_row < 0:
            for index, cell in enumerate(lowered):
                if "name" in cell or "principal" in cell or "position" in cell:
                    name_index = index
                    name_header_row = row_index
                    break
        if salary_header_row < 0:
            salary_match = next((idx for idx, cell in enumerate(lowered) if "salary" in cell), -1)
            if salary_match >= 0:
                salary_index = salary_match
                salary_header_row = row_index
        if total_header_row < 0:
            total_match = next((idx for idx, cell in enumerate(lowered) if "total" in cell), -1)
            if total_match >= 0:
                total_index = total_match
                total_header_row = row_index
        if title_index < 0:
            for idx, cell in enumerate(lowered):
                if idx == name_index:
                    continue
                if "title" in cell or "position" in cell:
                    title_index = idx
                    break

    if salary_index < 0:
        return -1, 0, -1, -1, -1

    header_index = max(name_header_row, salary_header_row, total_header_row, 0)
    return header_index, name_index, title_index, salary_index, total_index


def _normalize_name(raw_name: str) -> str:
    no_marks = _FOOTNOTE_MARK_RE.sub("", raw_name.strip())
    return re.sub(r"\s+", " ", no_marks)


def _split_name_and_title(name_cell: str, title_cell: str) -> tuple[str, str]:
    cleaned_name_cell = _normalize_name(name_cell)
    cleaned_title_cell = _normalize_name(title_cell)
    if cleaned_title_cell:
        return cleaned_name_cell, cleaned_title_cell
    if "," in cleaned_name_cell:
        left, right = cleaned_name_cell.split(",", maxsplit=1)
        return left.strip(), right.strip()
    return cleaned_name_cell, ""


def _role_from_text(text: str) -> str | None:
    lowered = text.lower()
    if "chief executive officer" in lowered or re.search(r"\bceo\b", lowered):
        return "ceo"
    if (
        "chief financial officer" in lowered
        or "principal financial officer" in lowered
        or re.search(r"\bcfo\b", lowered)
    ):
        return "cfo"
    if "chief operating officer" in lowered or re.search(r"\bcoo\b", lowered):
        return "coo"
    return None


def _to_float(value: str | None) -> float:
    if value is None:
        return float("-inf")
    try:
        return float(value)
    except ValueError:
        return float("-inf")


def _empty_role_dict() -> dict[str, str | None]:
    return {"name": "", "title": "", "salary": None, "total": None}


def _extract_numeric_from_row(row: list[str], index: int) -> str | None:
    if index < 0:
        return None
    candidate_indexes = [index, index + 1, index - 1, index + 2]
    for candidate_index in candidate_indexes:
        if candidate_index < 0 or candidate_index >= len(row):
            continue
        cleaned = _clean_numeric_text(row[candidate_index])
        if cleaned is not None:
            return cleaned
    return None


def _extract_last_numeric(row: list[str]) -> str | None:
    for cell in reversed(row):
        cleaned = _clean_numeric_text(cell)
        if cleaned is None:
            continue
        try:
            numeric_value = abs(float(cleaned))
        except ValueError:
            continue
        if numeric_value <= 2100 and len(cleaned.replace("-", "")) <= 4:
            continue
        return cleaned
    return None


def _heuristic_extract(table_text: str, reason: str) -> dict[str, object]:
    rows = _extract_rows_from_table_text(table_text)
    header_index, name_index, title_index, salary_index, total_index = _find_table_indices(rows)

    candidates: list[dict[str, str | None]] = []
    if header_index >= 0 and salary_index >= 0:
        for row in rows[header_index + 1 :]:
            max_required_index = max(name_index, salary_index)
            if len(row) <= max_required_index:
                continue
            name_cell = row[name_index]
            title_cell = row[title_index] if title_index >= 0 and title_index < len(row) else ""
            name, title = _split_name_and_title(name_cell, title_cell)
            salary = _extract_numeric_from_row(row, salary_index)
            total = _extract_numeric_from_row(row, total_index)
            if total is None:
                total = _extract_last_numeric(row)
            if not name and not title:
                continue
            if salary is None and total is None:
                continue
            candidates.append(
                {
                    "name": name,
                    "title": title,
                    "salary": salary,
                    "total": total,
                }
            )

    by_role: dict[str, dict[str, str | None]] = {
        "ceo": _empty_role_dict(),
        "cfo": _empty_role_dict(),
        "coo": _empty_role_dict(),
    }
    used_names: set[str] = set()
    for candidate in sorted(candidates, key=lambda item: _to_float(item.get("total")), reverse=True):
        role = _role_from_text(f"{candidate.get('name', '')} {candidate.get('title', '')}")
        if role is None:
            continue
        existing = by_role[role]
        if _to_float(candidate.get("total")) > _to_float(existing.get("total")):
            by_role[role] = candidate
            if candidate.get("name"):
                used_names.add(str(candidate["name"]))

    others: list[dict[str, str | None]] = []
    for candidate in sorted(candidates, key=lambda item: _to_float(item.get("total")), reverse=True):
        name = str(candidate.get("name", ""))
        if name and name in used_names:
            continue
        others.append(candidate)
        if len(others) >= _MAX_OTHERS:
            break

    confidence = 0.2
    if header_index >= 0:
        confidence += 0.2
    if by_role["ceo"].get("total"):
        confidence += 0.2
    if by_role["cfo"].get("total"):
        confidence += 0.1
    if by_role["coo"].get("total"):
        confidence += 0.1
    confidence += min(len(others), _MAX_OTHERS) * 0.05
    confidence = max(0.0, min(confidence, 0.85))

    return {
        "ceo": by_role["ceo"],
        "cfo": by_role["cfo"],
        "coo": by_role["coo"],
        "others": others,
        "confidence": confidence,
        "notes": f"heuristic_fallback:{reason}",
    }


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
        log.warning("OPENAI_API_KEY missing; using heuristic fallback extraction")
        return _heuristic_extract(table_text, "missing_openai_api_key")

    parsed_date = date.fromisoformat(filing_date[:10]) if filing_date else None
    filing_date_iso = parsed_date.isoformat() if parsed_date is not None else filing_date

    client = OpenAI(api_key=api_key, max_retries=0)
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
        try:
            response = client.chat.completions.create(
                model=model,
                temperature=0.0,
                response_format={"type": "json_object"},
                messages=messages,
            )  # type: ignore[call-overload]
        except Exception as exc:  # pragma: no cover - network/runtime path
            log.warning("OpenAI call failed; using heuristic fallback extraction: %s", exc)
            return _heuristic_extract(table_text, f"openai_call_failed:{type(exc).__name__}")

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
    log.warning("LLM response unusable after retries; using heuristic fallback extraction: %s", last_error)
    return _heuristic_extract(table_text, f"validation_failed:{type(last_error).__name__}")
