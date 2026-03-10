"""Extract structured compensation tables from parsed SEC proxy blocks."""
from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping
from typing import Any

from ingestion.metadata_model import BaseBlock, FootnoteBlock, HeadingBlock, ProseBlock, TableBlock

log = logging.getLogger(__name__)

_TABLE_SIGNATURES: dict[str, list[str]] = {
    "summary_compensation": [
        "summary compensation table",
        "summary compensation",
        "named executive officer compensation",
        "compensation of named executive officers",
        "annual compensation",
        "total compensation",
    ],
    "equity_awards": [
        "outstanding equity awards at fiscal year",
        "outstanding equity awards at year",
        "outstanding equity awards",
        "unexercised options",
        "equity awards outstanding",
    ],
    "grants_plan_based": [
        "grants of plan-based awards",
        "grants of plan based awards",
        "plan-based award grants",
        "incentive plan awards",
        "fiscal year grants",
    ],
    "option_exercises": [
        "option exercises and stock vested",
        "options exercised and stock vested",
        "option exercises and stock awards vested",
        "exercised options and vested stock",
        "stock option exercises",
    ],
    "pension_benefits": [
        "pension benefits",
        "defined benefit",
        "supplemental executive retirement",
        "retirement benefits",
        "nonqualified deferred compensation",
        "serp",
    ],
}

NUMERIC_COLUMNS = {
    "salary",
    "bonus",
    "stock_awards",
    "option_awards",
    "non_equity_incentive",
    "pension_change",
    "other_comp",
    "total",
    "grant_fair_value",
    "threshold",
    "target",
    "maximum",
    "options_value",
    "stock_vested_value",
    "present_value",
    "payments",
    "exercise_price",
    "stock_awards_unvested_value",
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
}

_SUMMARY_COMP_COLS: dict[str, list[str]] = {
    "exec_name": [
        "name and principal position",
        "name",
        "executive",
    ],
    "exec_title": [
        "principal position",
        "title",
        "position",
    ],
    "year": ["year", "fiscal year"],
    "salary": ["salary", "base salary"],
    "bonus": ["bonus", "cash bonus"],
    "stock_awards": ["stock awards", "stock award", "restricted stock", "rsu", "dsu"],
    "option_awards": ["option awards", "option award", "options"],
    "non_equity_incentive": [
        "non-equity incentive",
        "non equity incentive",
        "nonequity incentive",
        "non-equity incentive plan compensation",
        "nonequity incentive plan compensation",
        "annual incentive",
    ],
    "pension_change": ["change in pension", "pension value", "nonqualified deferred"],
    "other_comp": ["all other compensation", "all other comp", "other compensation"],
    "total": ["total"],
}

_SUMMARY_COMP_REQUIRED_NUMERIC_COLS = {
    "salary",
    "bonus",
    "stock_awards",
    "option_awards",
    "non_equity_incentive",
    "pension_change",
    "other_comp",
    "total",
}

_NUMERIC_EMPTY_MARKERS = {"", "—", "-", "$", "n/a", "na"}
_EXEC_TITLE_KEYWORDS = {
    "officer",
    "president",
    "director",
    "chair",
    "chairman",
    "chairwoman",
    "executive",
    "ceo",
    "cfo",
    "coo",
    "chief",
    "vice president",
    "svp",
    "evp",
    "vp",
    "general counsel",
    "treasurer",
    "secretary",
}
_EXEC_TITLE_LEAD_TOKENS = {
    "chief",
    "president",
    "chair",
    "chairman",
    "chairwoman",
    "director",
    "executive",
    "senior",
    "vice",
    "interim",
    "acting",
    "lead",
    "principal",
    "general",
    "co-chief",
    "co-president",
}

_EQUITY_AWARDS_COLS: dict[str, list[str]] = {
    "exec_name": ["name", "executive"],
    "option_grant_date": ["grant date", "option grant"],
    "options_unexercisable": ["unexercisable", "unvested options"],
    "options_exercisable": ["exercisable", "vested options"],
    "exercise_price": ["exercise price", "option exercise price"],
    "expiration_date": ["expiration", "option expiration"],
    "stock_awards_unvested_shares": ["unvested shares", "number of shares", "shares not vested"],
    "stock_awards_unvested_value": ["market value", "unvested value"],
}

_GRANTS_COLS: dict[str, list[str]] = {
    "exec_name": ["name", "executive"],
    "grant_date": ["grant date"],
    "grant_type": ["grant type", "award type", "type of award"],
    "non_equity_threshold": ["non-equity threshold", "non equity threshold"],
    "non_equity_target": ["non-equity target", "non equity target"],
    "non_equity_maximum": ["non-equity maximum", "non equity maximum"],
    "equity_threshold": ["equity threshold"],
    "equity_target": ["equity target"],
    "equity_maximum": ["equity maximum"],
    "all_other_stock_awards_shares": ["all other stock awards", "number of shares of stock or units"],
    "all_other_option_awards_securities": ["all other option awards", "number of securities underlying options"],
    "exercise_or_base_price": ["exercise or base price", "exercise price", "base price"],
    "grant_date_fair_value": ["grant date fair value", "fair value"],
}

_GRANTS_NAME_HEADER_HINTS = {
    "name",
    "named executive officer",
}
_GRANTS_GRANT_TYPE_HINTS = {
    "grant type",
    "award type",
    "type of award",
}
_GRANTS_NON_EQUITY_HINTS = {
    "non-equity incentive plan award",
    "non equity incentive plan award",
}
_GRANTS_EQUITY_HINTS = {
    "equity incentive plan award",
}
_GRANTS_TYPE_ROW_HINTS = {
    "incentive plan",
    "annual incentive award",
    "annual stip bonus",
    "stip bonus",
    "performance restricted stock unit",
    "performance-based rsu",
    "performance based rsu",
    "prsu",
    "annual psu grant",
    "psu grant",
    "annual rsu grant",
    "rsu grant",
    "time-based rsu",
    "time based rsu",
    "time-lapse rsu",
    "time lapse rsu",
    "stock option",
}

_OPTION_EXERCISES_COLS: dict[str, list[str]] = {
    "exec_name": ["name", "executive"],
    "options_exercised": ["options exercised", "shares acquired on exercise", "number exercised"],
    "options_value": ["value realized on exercise", "value realized", "exercise value"],
    "stock_vested_shares": ["shares acquired on vesting", "stock vested", "shares vested"],
    "stock_vested_value": ["value realized on vesting", "vesting value"],
}

_PENSION_COLS: dict[str, list[str]] = {
    "exec_name": ["name", "executive"],
    "plan_name": ["plan name", "plan"],
    "years_credited": ["years of credited service", "credited service", "years of service"],
    "present_value": ["present value", "actuarial present value"],
    "payments": ["payments during last fiscal year", "payments"],
}


def _normalise(value: str) -> str:
    # Normalize wrapped header tokens like "Compensa-tion" -> "compensation".
    compact = value.replace("\u00ad", "")
    compact = re.sub(r"(?<=\w)-\s*(?=\w)", "", compact)
    return re.sub(r"\s+", " ", compact.strip().lower())


def clean_numeric(val: str) -> float | None:
    """Strip currency formatting and return float where possible."""
    if not val or val.strip() in ("", "—", "-", "N/A", "na", "n/a"):
        return None

    cleaned = re.sub(r"[$,\s]", "", val.strip())
    cleaned = re.sub(r"^\((.+)\)$", r"-\1", cleaned)
    try:
        return float(cleaned)
    except ValueError:
        return None


def _heading_matches(heading_text: str, signatures: list[str]) -> bool:
    heading = _normalise(heading_text)
    return any(_normalise(signature) in heading for signature in signatures)


def _match_col(header: str, mapping: dict[str, list[str]]) -> str | None:
    normalized = _normalise(header)
    for canonical, variants in mapping.items():
        if any(variant in normalized for variant in variants):
            return canonical
    return None


def _index_headings(blocks: list[BaseBlock]) -> dict[str, HeadingBlock]:
    return {
        block.id: block
        for block in blocks
        if isinstance(block, HeadingBlock)
    }


def _index_footnotes(blocks: list[BaseBlock]) -> dict[str, dict[str, str]]:
    footnotes_by_table: dict[str, dict[str, str]] = {}
    for block in blocks:
        if not isinstance(block, FootnoteBlock):
            continue
        if not block.linked_table_id:
            continue
        table_footnotes = footnotes_by_table.setdefault(block.linked_table_id, {})
        table_footnotes[block.marker] = block.text
    return footnotes_by_table


def _resolve_source_heading(
    blocks: list[BaseBlock],
    table_index: int,
    table_block: TableBlock,
    signatures: list[str],
    heading_by_id: dict[str, HeadingBlock],
) -> str | None:
    section_heading = heading_by_id.get(table_block.section_id)
    if section_heading is not None and _heading_matches(section_heading.text, signatures):
        return section_heading.text

    start = max(0, table_index - 12)
    for index in range(table_index - 1, start - 1, -1):
        block = blocks[index]
        if isinstance(block, HeadingBlock) and _heading_matches(block.text, signatures):
            return block.text
    return None


def _build_column_map(
    table_block: TableBlock,
    schema: dict[str, list[str]],
    allow_duplicates: set[str] | None = None,
    header_rows: int | None = None,
) -> list[str | None]:
    if not table_block.rows:
        return []

    resolved_header_rows = (
        header_rows
        if header_rows is not None
        else (table_block.header_row_count if table_block.header_row_count > 0 else 1)
    )
    resolved_header_rows = min(max(1, resolved_header_rows), len(table_block.rows))
    header_slice = table_block.rows[:resolved_header_rows]
    column_count = max((len(row) for row in header_slice), default=0)

    column_map: list[str | None] = []
    seen: set[str] = set()
    for column_index in range(column_count):
        parts: list[str] = []
        for row in header_slice:
            if column_index >= len(row):
                continue
            cell = row[column_index].strip()
            if cell and cell not in parts:
                parts.append(cell)
        header_text = " ".join(parts)
        canonical = _match_col(header_text, schema) if header_text else None
        if canonical in seen and (allow_duplicates is None or canonical not in allow_duplicates):
            canonical = None
        if canonical is not None:
            seen.add(canonical)
        column_map.append(canonical)
    return column_map


def _contains_any(normalized_text: str, hints: set[str]) -> bool:
    return any(hint in normalized_text for hint in hints)


def _non_empty_cells(row: list[str]) -> list[str]:
    return [cell.strip() for cell in row if cell.strip()]


def _row_text(row: list[str]) -> str:
    return _normalise(" ".join(_non_empty_cells(row)))


def _header_tokens(text: str) -> set[str]:
    tokens = set(re.findall(r"[a-z0-9]+", text))
    if "nonequity" in tokens:
        tokens.update({"non", "equity"})
    if "planbased" in tokens:
        tokens.update({"plan", "based"})
    return tokens


def _has_tokens(text: str, *required: str) -> bool:
    tokens = _header_tokens(text)
    return all(token in tokens for token in required)


def _is_non_equity_grants_context(text: str) -> bool:
    tokens = _header_tokens(text)
    return (
        ("nonequity" in tokens or "non" in tokens)
        and all(token in tokens for token in ("equity", "incentive", "plan"))
        and ("award" in tokens or "awards" in tokens)
    )


def _is_equity_grants_context(text: str) -> bool:
    tokens = _header_tokens(text)
    return (
        "nonequity" not in tokens
        and "non" not in tokens
        and all(token in tokens for token in ("equity", "incentive", "plan"))
        and ("award" in tokens or "awards" in tokens)
    )


def _infer_grants_header_rows(table_block: TableBlock) -> int:
    """Infer grants header depth even when parser reports header_row_count=0."""
    if not table_block.rows:
        return 0

    max_scan = min(12, len(table_block.rows))
    subheader_index: int | None = None
    triplet_row_index: int | None = None
    name_date_row_index: int | None = None
    for index in range(max_scan):
        text = _row_text(table_block.rows[index])
        if not text:
            continue
        row_tokens = _header_tokens(text)
        has_name = ("name" in text) or ("named executive officer" in text)
        has_grant_date = ("grant date" in text) or _has_tokens(text, "grant", "date")
        has_date_signal = has_grant_date or ("date" in row_tokens)
        has_triplet = any(token in row_tokens for token in ("threshold", "target", "maximum"))
        if has_name and has_grant_date and has_triplet:
            subheader_index = index
            break
        if has_triplet and triplet_row_index is None:
            triplet_row_index = index
        if has_name and has_date_signal and name_date_row_index is None:
            name_date_row_index = index

    if (
        subheader_index is None
        and triplet_row_index is not None
        and name_date_row_index is not None
        and abs(triplet_row_index - name_date_row_index) <= 4
    ):
        subheader_index = max(triplet_row_index, name_date_row_index)

    if subheader_index is not None:
        return max(1, subheader_index + 1)

    # Fallback when no clear subheader row is detected.
    if table_block.header_row_count > 0:
        return min(max(1, table_block.header_row_count), len(table_block.rows))
    return min(4, len(table_block.rows))


def _infer_summary_header_rows(table_block: TableBlock) -> int:
    """Infer summary compensation header depth when parser reports 0."""
    if not table_block.rows:
        return 0

    max_scan = min(8, len(table_block.rows))
    for index in range(max_scan):
        text = _row_text(table_block.rows[index])
        if not text:
            continue
        if "name" in text and "year" in text and ("salary" in text or "total" in text):
            return max(1, index + 1)

    if table_block.header_row_count > 0:
        return min(max(1, table_block.header_row_count), len(table_block.rows))
    return min(2, len(table_block.rows))


def _grants_data_rows_with_index(table_block: TableBlock) -> list[tuple[int, list[str]]]:
    if not table_block.rows:
        return []
    header_rows = _infer_grants_header_rows(table_block)
    start_index = min(header_rows, len(table_block.rows))
    return list(enumerate(table_block.rows[start_index:], start=start_index))


def _build_grants_column_map(
    table_block: TableBlock,
    schema: dict[str, list[str]],
    header_rows: int | None = None,
) -> list[str | None]:
    """Build column mapping for Grants of Plan-Based Awards with semantic triplet split."""
    if not table_block.rows:
        return []

    resolved_header_rows = header_rows if header_rows is not None else _infer_grants_header_rows(table_block)
    resolved_header_rows = min(max(1, resolved_header_rows), len(table_block.rows), 12)
    header_slice = table_block.rows[:resolved_header_rows]
    column_count = max((len(row) for row in header_slice), default=0)

    group_context: list[str | None] = [None] * column_count
    for row in header_slice:
        last_group: str | None = None
        for column_index in range(column_count):
            cell = _normalise(row[column_index]) if column_index < len(row) else ""
            if _is_non_equity_grants_context(cell):
                last_group = "non_equity"
                group_context[column_index] = "non_equity"
                continue
            if _is_equity_grants_context(cell):
                last_group = "equity"
                group_context[column_index] = "equity"
                continue
            if not cell and last_group is not None and group_context[column_index] is None:
                group_context[column_index] = last_group

    column_map: list[str | None] = []

    for column_index in range(column_count):
        column_cells: list[str] = []
        for row in header_slice:
            if column_index >= len(row):
                continue
            cell = row[column_index].strip()
            if cell:
                column_cells.append(cell)
        combined_header = _normalise(" ".join(column_cells))

        canonical: str | None = None
        if not combined_header:
            column_map.append(None)
            continue

        if _contains_any(combined_header, _GRANTS_NAME_HEADER_HINTS):
            canonical = "exec_name"
        elif _contains_any(combined_header, _GRANTS_GRANT_TYPE_HINTS):
            canonical = "grant_type"
        elif "grant date fair value" in combined_header or (
            "fair value" in combined_header and ("grant date" in combined_header or _has_tokens(combined_header, "grant", "date"))
        ):
            canonical = "grant_date_fair_value"
        elif "grant date" in combined_header or _has_tokens(combined_header, "grant", "date"):
            canonical = "grant_date"
        elif "all other stock awards" in combined_header or (
            "stock awards" in combined_header and "number of shares" in combined_header
        ):
            canonical = "all_other_stock_awards_shares"
        elif "all other option awards" in combined_header or "securities underlying options" in combined_header:
            canonical = "all_other_option_awards_securities"
        elif "exercise or base price" in combined_header or "exercise price" in combined_header:
            canonical = "exercise_or_base_price"
        elif "threshold" in combined_header:
            if _is_non_equity_grants_context(combined_header):
                canonical = "non_equity_threshold"
            elif _is_equity_grants_context(combined_header):
                canonical = "equity_threshold"
            elif group_context[column_index] == "non_equity":
                canonical = "non_equity_threshold"
            elif group_context[column_index] == "equity":
                canonical = "equity_threshold"
        elif "target" in combined_header:
            if _is_non_equity_grants_context(combined_header):
                canonical = "non_equity_target"
            elif _is_equity_grants_context(combined_header):
                canonical = "equity_target"
            elif group_context[column_index] == "non_equity":
                canonical = "non_equity_target"
            elif group_context[column_index] == "equity":
                canonical = "equity_target"
        elif "maximum" in combined_header or " max " in f" {combined_header} ":
            if _is_non_equity_grants_context(combined_header):
                canonical = "non_equity_maximum"
            elif _is_equity_grants_context(combined_header):
                canonical = "equity_maximum"
            elif group_context[column_index] == "non_equity":
                canonical = "non_equity_maximum"
            elif group_context[column_index] == "equity":
                canonical = "equity_maximum"
        else:
            canonical = _match_col(combined_header, schema)

        column_map.append(canonical)

    return column_map


def _data_rows(table_block: TableBlock) -> list[list[str]]:
    if not table_block.rows:
        return []
    start = table_block.header_row_count if table_block.header_row_count > 0 else 1
    return table_block.rows[min(start, len(table_block.rows)) :]


def _summary_table_has_comp_columns(column_map: list[str | None]) -> bool:
    mapped = {column for column in column_map if column is not None}
    return bool(mapped & _SUMMARY_COMP_REQUIRED_NUMERIC_COLS)


def _collect_table_footnotes(
    table_block: TableBlock,
    indexed_footnotes: dict[str, dict[str, str]],
) -> dict[str, str]:
    merged = dict(table_block.footnotes)
    merged.update(indexed_footnotes.get(table_block.id, {}))
    return merged


def _extract_row_footnote_refs(row: list[str], footnotes: dict[str, str]) -> str:
    refs: list[str] = []
    for marker, text in footnotes.items():
        if any(marker in cell for cell in row):
            refs.append(f"{marker}: {text[:120]}")
    return " || ".join(refs)


def _normalize_grants_row_cells(row: list[str]) -> list[str]:
    """Normalize split currency symbol + numeric cells for grants rows."""
    normalized = [cell.strip() for cell in row]
    for idx in range(len(normalized) - 1):
        left = normalized[idx]
        right = normalized[idx + 1]
        if left == "$" and right and any(char.isdigit() for char in right) and not right.startswith("$"):
            normalized[idx] = ""
            normalized[idx + 1] = f"${right}"
    return normalized


def _grants_candidate_indices(column_map: list[str | None]) -> dict[str, list[int]]:
    candidates: dict[str, list[int]] = {}
    for index, canonical in enumerate(column_map):
        if canonical is None:
            continue
        candidates.setdefault(canonical, []).append(index)
    return candidates


def _select_best_grants_cell(row: list[str], indices: list[int]) -> str:
    values: list[str] = []
    for index in indices:
        if index >= len(row):
            continue
        value = row[index].strip()
        if value:
            values.append(value)
    if not values:
        return ""

    def _rank(value: str) -> tuple[int, int]:
        if any(char.isdigit() for char in value):
            return (3, len(value))
        if value in {"$", "—", "-", "n/a", "na"}:
            return (1, len(value))
        return (2, len(value))

    return max(values, key=_rank)


def _map_grants_row(
    row: list[str],
    column_map: list[str | None],
    metadata: Mapping[str, Any],
    footnotes: dict[str, str],
    source_section: str,
    table_block_id: str,
) -> dict[str, Any]:
    output: dict[str, Any] = dict(metadata)
    normalized_row = _normalize_grants_row_cells(row)
    candidates = _grants_candidate_indices(column_map)
    mapped_values = 0
    for canonical, indices in candidates.items():
        value = _select_best_grants_cell(normalized_row, indices)
        if not value:
            continue
        output[canonical] = value
        if canonical in NUMERIC_COLUMNS:
            numeric_val = clean_numeric(value)
            output[canonical] = numeric_val if numeric_val is not None else value
        mapped_values += 1

    if mapped_values == 0 and normalized_row:
        output["exec_name"] = normalized_row[0].strip()

    output["footnote_refs"] = _extract_row_footnote_refs(normalized_row, footnotes)
    output["source_section"] = source_section
    output["table_block_id"] = table_block_id
    return output


def _looks_like_exec_title(value: str) -> bool:
    lowered = value.lower()
    return any(keyword in lowered for keyword in _EXEC_TITLE_KEYWORDS)


def _split_exec_name_and_title(value: str) -> tuple[str, str]:
    raw_value = value.strip()
    if not raw_value:
        return "", ""

    line_parts = [part.strip(" ,") for part in re.split(r"[\r\n]+", raw_value) if part.strip(" ,")]
    if len(line_parts) >= 2:
        title_candidate = " ".join(line_parts[1:]).strip()
        if _looks_like_exec_title(title_candidate):
            return line_parts[0], title_candidate

    normalized = re.sub(r"\s+", " ", raw_value).strip(" ,")
    if "," in normalized:
        name_part, _, title_part = normalized.partition(",")
        title_candidate = title_part.strip()
        if _looks_like_exec_title(title_candidate):
            return name_part.strip(), title_candidate

    tokens = normalized.split()
    for index in range(2, len(tokens)):
        token = re.sub(r"^[^A-Za-z]+|[^A-Za-z-]+$", "", tokens[index]).lower()
        if token not in _EXEC_TITLE_LEAD_TOKENS:
            continue
        name_candidate = " ".join(tokens[:index]).strip(" ,")
        title_candidate = " ".join(tokens[index:]).strip(" ,")
        if name_candidate and _looks_like_exec_title(title_candidate):
            return name_candidate, title_candidate

    return normalized, ""


def _normalize_summary_numeric_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if isinstance(value, float):
            if value.is_integer():
                return str(int(value))
            return format(value, "f").rstrip("0").rstrip(".")
        return str(value)

    text = str(value).strip()
    if not text or text.lower() in _NUMERIC_EMPTY_MARKERS:
        return None

    cleaned = re.sub(r"[$,\s]", "", text)
    cleaned = re.sub(r"^\((.+)\)$", r"-\1", cleaned)
    if re.fullmatch(r"-?\d+(\.\d+)?", cleaned):
        if cleaned.endswith(".0"):
            return cleaned[:-2]
        return cleaned
    return text


def _summary_row_has_payload(row: Mapping[str, Any]) -> bool:
    if str(row.get("year", "") or "").strip():
        return True
    for field in _SUMMARY_COMP_REQUIRED_NUMERIC_COLS:
        if _normalize_summary_numeric_value(row.get(field)) is not None:
            return True
    return False


def _map_row(
    row: list[str],
    column_map: list[str | None],
    metadata: Mapping[str, Any],
    footnotes: dict[str, str],
    source_section: str,
    table_block_id: str,
) -> dict[str, Any]:
    output: dict[str, Any] = dict(metadata)
    mapped_values = 0
    for index, cell in enumerate(row):
        if index >= len(column_map):
            continue
        canonical = column_map[index]
        if canonical is None:
            continue
        value = cell.strip()
        if canonical in NUMERIC_COLUMNS:
            numeric_val = clean_numeric(value)
            existing = output.get(canonical)
            existing_text = str(existing or "").strip().lower()

            if numeric_val is not None:
                output[canonical] = numeric_val
            elif value.strip().lower() in _NUMERIC_EMPTY_MARKERS:
                if existing is None or existing_text in _NUMERIC_EMPTY_MARKERS:
                    output[canonical] = ""
            elif existing is None or existing_text in _NUMERIC_EMPTY_MARKERS:
                output[canonical] = value
        else:
            output[canonical] = value
        mapped_values += 1

    if mapped_values == 0 and row:
        output["exec_name"] = row[0].strip()

    # Split "Name and Principal Position" cells into name + title when possible.
    raw_exec = str(output.get("exec_name", "") or "")
    if raw_exec and not output.get("exec_title"):
        exec_name, exec_title = _split_exec_name_and_title(raw_exec)
        output["exec_name"] = exec_name
        if exec_title:
            output["exec_title"] = exec_title

    output["footnote_refs"] = _extract_row_footnote_refs(row, footnotes)
    output["source_section"] = source_section
    output["table_block_id"] = table_block_id
    return output


def _is_title_only_exec_name(value: str) -> bool:
    normalized = _normalise(value)
    if not normalized or any(char.isdigit() for char in normalized):
        return False
    return _looks_like_exec_title(normalized) and len(normalized.split()) <= 10


def _normalize_summary_comp_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return []

    current_name_by_table: dict[str, str] = {}
    last_person_row_by_table: dict[str, dict[str, Any]] = {}
    normalized_rows: list[dict[str, Any]] = []

    for row in rows:
        out = dict(row)
        table_id = str(out.get("table_block_id", "") or "")
        exec_name = str(out.get("exec_name", "") or "").strip()
        exec_title = str(out.get("exec_title", "") or "").strip()

        if exec_name and not exec_title:
            exec_name, exec_title = _split_exec_name_and_title(exec_name)
            out["exec_name"] = exec_name
            if exec_title:
                out["exec_title"] = exec_title

        if not exec_name and _summary_row_has_payload(out):
            prior_row = last_person_row_by_table.get(table_id)
            if prior_row is not None:
                prior_name = str(prior_row.get("exec_name", "") or "").strip()
                prior_title = str(prior_row.get("exec_title", "") or "").strip()
                if prior_name:
                    out["exec_name"] = prior_name
                    if prior_title and not str(out.get("exec_title", "") or "").strip():
                        out["exec_title"] = prior_title
                    exec_name = prior_name
                    exec_title = str(out.get("exec_title", "") or "").strip()

        is_title_only = bool(exec_name) and not exec_title and _is_title_only_exec_name(exec_name)
        if is_title_only:
            prior_name = current_name_by_table.get(table_id, "")
            if prior_name:
                inferred_title = exec_name
                out["exec_name"] = prior_name
                out["exec_title"] = inferred_title

                prior_row = last_person_row_by_table.get(table_id)
                if prior_row is not None and not str(prior_row.get("exec_title", "") or "").strip():
                    prior_row["exec_title"] = inferred_title
        elif exec_name:
            current_name_by_table[table_id] = exec_name
            last_person_row_by_table[table_id] = out

        for field in _SUMMARY_COMP_REQUIRED_NUMERIC_COLS:
            out[field] = _normalize_summary_numeric_value(out.get(field))

        normalized_rows.append(out)

    return normalized_rows


def _extract_table(
    blocks: list[BaseBlock],
    signatures: list[str],
    col_schema: dict[str, list[str]],
    meta: Mapping[str, Any],
    column_mapper: Callable[[TableBlock, dict[str, list[str]]], list[str | None]] | None = None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    heading_by_id = _index_headings(blocks)
    footnotes_by_table = _index_footnotes(blocks)

    for index, block in enumerate(blocks):
        if not isinstance(block, TableBlock):
            continue
        source_heading = _resolve_source_heading(
            blocks,
            index,
            block,
            signatures,
            heading_by_id,
        )
        if source_heading is None:
            continue

        resolved_column_mapper = column_mapper or _build_column_map
        column_map = resolved_column_mapper(block, col_schema)
        if not column_map:
            log.debug("No mapped columns for table block %s under heading '%s'", block.id, source_heading)

        footnotes = _collect_table_footnotes(block, footnotes_by_table)
        for row in _data_rows(block):
            if not any(cell.strip() for cell in row):
                continue
            results.append(
                _map_row(
                    row=row,
                    column_map=column_map,
                    metadata=meta,
                    footnotes=footnotes,
                    source_section=source_heading,
                    table_block_id=block.id,
                )
            )
    return results


def _resolve_summary_source_section(blocks: list[BaseBlock], table_block: TableBlock) -> str:
    heading_by_id = _index_headings(blocks)
    for index, block in enumerate(blocks):
        if not isinstance(block, TableBlock) or block.id != table_block.id:
            continue
        source_heading = _resolve_source_heading(
            blocks,
            index,
            table_block,
            _TABLE_SIGNATURES["summary_compensation"],
            heading_by_id,
        )
        if source_heading is not None:
            return source_heading
        break
    return "summary compensation table"


def _extract_summary_from_table_block(
    blocks: list[BaseBlock],
    table_block: TableBlock,
    meta: Mapping[str, Any],
) -> list[dict[str, Any]]:
    header_rows = _infer_summary_header_rows(table_block)
    column_map = _build_column_map(
        table_block,
        _SUMMARY_COMP_COLS,
        allow_duplicates=_SUMMARY_COMP_REQUIRED_NUMERIC_COLS,
        header_rows=header_rows,
    )
    if not column_map or not _summary_table_has_comp_columns(column_map):
        return []

    footnotes_by_table = _index_footnotes(blocks)
    footnotes = _collect_table_footnotes(table_block, footnotes_by_table)
    source_section = _resolve_summary_source_section(blocks, table_block)

    rows_out: list[dict[str, Any]] = []
    data_start = min(max(1, header_rows), len(table_block.rows))
    for row in table_block.rows[data_start:]:
        if not any(cell.strip() for cell in row):
            continue
        rows_out.append(
            _map_row(
                row=row,
                column_map=column_map,
                metadata=meta,
                footnotes=footnotes,
                source_section=source_section,
                table_block_id=table_block.id,
            )
        )
    return rows_out


def extract_summary_compensation(
    blocks: list[BaseBlock],
    meta: Mapping[str, Any],
    *,
    selected_table: TableBlock | None = None,
) -> list[dict[str, Any]]:
    """Extract Summary Compensation Table rows.

    When ``selected_table`` is provided, only rows originating from that
    specific table block are returned.
    """
    raw_rows = _extract_table(
        blocks,
        _TABLE_SIGNATURES["summary_compensation"],
        _SUMMARY_COMP_COLS,
        meta,
        column_mapper=lambda block, schema: _build_column_map(
            block,
            schema,
            allow_duplicates=_SUMMARY_COMP_REQUIRED_NUMERIC_COLS,
        ),
    )
    if selected_table is not None:
        selected_id = selected_table.id
        has_selected_rows = any(str(row.get("table_block_id", "") or "") == selected_id for row in raw_rows)
        if not has_selected_rows:
            raw_rows.extend(_extract_summary_from_table_block(blocks, selected_table, meta))

    if not raw_rows:
        return []

    raw_rows = _normalize_summary_comp_rows(raw_rows)

    valid_rows: list[dict[str, Any]] = []
    by_table: dict[str, list[dict[str, Any]]] = {}
    for row in raw_rows:
        table_id = str(row.get("table_block_id", "") or "")
        if not table_id:
            continue
        by_table.setdefault(table_id, []).append(row)

    table_by_id = {
        block.id: block
        for block in blocks
        if isinstance(block, TableBlock)
    }
    for table_id, table_rows in by_table.items():
        table = table_by_id.get(table_id)
        if table is None:
            continue
        header_rows = _infer_summary_header_rows(table)
        column_map = _build_column_map(
            table,
            _SUMMARY_COMP_COLS,
            allow_duplicates=_SUMMARY_COMP_REQUIRED_NUMERIC_COLS,
            header_rows=header_rows,
        )
        if not _summary_table_has_comp_columns(column_map):
            continue
        valid_rows.extend(table_rows)

    if selected_table is None:
        return valid_rows

    selected_id = selected_table.id
    return [row for row in valid_rows if str(row.get("table_block_id", "") or "") == selected_id]


def extract_equity_awards(
    blocks: list[BaseBlock],
    meta: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Extract Outstanding Equity Awards table rows."""
    return _extract_table(
        blocks,
        _TABLE_SIGNATURES["equity_awards"],
        _EQUITY_AWARDS_COLS,
        meta,
    )


def _resolve_grants_source_section(blocks: list[BaseBlock], table_block: TableBlock) -> str:
    heading_by_id = _index_headings(blocks)
    for index, block in enumerate(blocks):
        if not isinstance(block, TableBlock) or block.id != table_block.id:
            continue
        source_heading = _resolve_source_heading(
            blocks,
            index,
            table_block,
            _TABLE_SIGNATURES["grants_plan_based"],
            heading_by_id,
        )
        if source_heading is not None:
            return source_heading

        start = max(0, index - 12)
        for probe_index in range(index - 1, start - 1, -1):
            probe = blocks[probe_index]
            probe_text = ""
            if isinstance(probe, HeadingBlock):
                probe_text = probe.text
            elif isinstance(probe, ProseBlock):
                probe_text = probe.text
            if probe_text and _heading_matches(probe_text, _TABLE_SIGNATURES["grants_plan_based"]):
                return probe_text
        break
    return "grants of plan-based awards"


def _is_grant_type_label(text: str) -> bool:
    normalized = _normalise(text)
    if not normalized:
        return False
    if any(hint in normalized for hint in _GRANTS_TYPE_ROW_HINTS):
        return True
    token_set = _header_tokens(normalized)
    if {"stock", "option"} <= token_set or "options" in token_set:
        return True
    if "rsu" in token_set or "rsus" in token_set or "restricted" in token_set:
        return True
    if "psu" in token_set or "psus" in token_set:
        return True
    return "bonus" in token_set and "annual" in token_set


def _grant_row_has_numeric_payload(row: Mapping[str, Any]) -> bool:
    for field in (
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
    ):
        value = row.get(field)
        if isinstance(value, (int, float)):
            return True
        text = str(value or "").strip()
        if text and any(char.isdigit() for char in text):
            return True
    return False


def _is_grants_name_only_row(row: Mapping[str, Any]) -> bool:
    exec_name = str(row.get("exec_name", "") or "").strip()
    if not exec_name:
        return False
    if _is_grant_type_label(exec_name):
        return False
    if str(row.get("grant_date", "") or "").strip():
        return False
    if str(row.get("grant_type", "") or "").strip():
        return False
    return not _grant_row_has_numeric_payload(row)


def _normalize_grants_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return []

    sorted_rows = sorted(
        rows,
        key=lambda row: (
            str(row.get("table_block_id", "") or ""),
            int(row.get("source_row_index", 0) or 0),
        ),
    )
    current_name_by_table: dict[str, str] = {}
    normalized_rows: list[dict[str, Any]] = []

    for row in sorted_rows:
        out = dict(row)
        table_id = str(out.get("table_block_id", "") or "")
        current_exec_name = current_name_by_table.get(table_id, "")
        raw_exec_name = str(out.get("exec_name", "") or "").strip()
        raw_grant_type = str(out.get("grant_type", "") or "").strip()

        if _is_grants_name_only_row(out):
            current_name_by_table[table_id] = raw_exec_name
            continue

        if raw_exec_name:
            if _is_grant_type_label(raw_exec_name):
                if not raw_grant_type:
                    out["grant_type"] = raw_exec_name
                if current_exec_name:
                    out["exec_name"] = current_exec_name
            else:
                current_name_by_table[table_id] = raw_exec_name
        elif current_exec_name:
            out["exec_name"] = current_exec_name

        if not str(out.get("grant_type", "") or "").strip() and _is_grant_type_label(raw_exec_name):
            out["grant_type"] = raw_exec_name

        normalized_rows.append(out)

    return normalized_rows


def _extract_grants_from_table_block(
    table_block: TableBlock,
    footnotes_by_table: dict[str, dict[str, str]],
    meta: Mapping[str, Any],
    source_section: str,
) -> list[dict[str, Any]]:
    header_rows = _infer_grants_header_rows(table_block)
    column_map = _build_grants_column_map(table_block, _GRANTS_COLS, header_rows=header_rows)
    if not column_map:
        return []

    footnotes = _collect_table_footnotes(table_block, footnotes_by_table)
    rows_out: list[dict[str, Any]] = []
    for source_row_index, row in _grants_data_rows_with_index(table_block):
        if not any(cell.strip() for cell in row):
            continue
        mapped = _map_grants_row(
            row=row,
            column_map=column_map,
            metadata=meta,
            footnotes=footnotes,
            source_section=source_section,
            table_block_id=table_block.id,
        )
        mapped["source_row_index"] = source_row_index
        rows_out.append(mapped)
    return rows_out


def extract_grants_plan_based(
    blocks: list[BaseBlock],
    meta: Mapping[str, Any],
    selected_table: TableBlock | None = None,
) -> list[dict[str, Any]]:
    """
    Extract Grants of Plan-Based Awards table rows.

    If ``selected_table`` is provided, it is parsed explicitly even when no
    matching heading block is present (common in prose-led filings).
    """
    heading_by_id = _index_headings(blocks)
    footnotes_by_table = _index_footnotes(blocks)

    heading_rows: list[dict[str, Any]] = []
    for index, block in enumerate(blocks):
        if not isinstance(block, TableBlock):
            continue
        source_heading = _resolve_source_heading(
            blocks,
            index,
            block,
            _TABLE_SIGNATURES["grants_plan_based"],
            heading_by_id,
        )
        if source_heading is None:
            continue
        heading_rows.extend(_extract_grants_from_table_block(block, footnotes_by_table, meta, source_heading))

    explicit_rows: list[dict[str, Any]] = []
    if selected_table is not None:
        source_section = _resolve_grants_source_section(blocks, selected_table)
        explicit_rows = _extract_grants_from_table_block(selected_table, footnotes_by_table, meta, source_section)

    merged = explicit_rows + heading_rows
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for row in merged:
        table_id = str(row.get("table_block_id", "") or "")
        row_index = int(row.get("source_row_index", -1) or -1)
        key = (table_id, row_index)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(row)

    return _normalize_grants_rows(deduped)


def extract_option_exercises(
    blocks: list[BaseBlock],
    meta: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Extract Option Exercises and Stock Vested table rows."""
    return _extract_table(
        blocks,
        _TABLE_SIGNATURES["option_exercises"],
        _OPTION_EXERCISES_COLS,
        meta,
    )


def extract_pension_benefits(
    blocks: list[BaseBlock],
    meta: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Extract Pension Benefits table rows."""
    return _extract_table(
        blocks,
        _TABLE_SIGNATURES["pension_benefits"],
        _PENSION_COLS,
        meta,
    )
