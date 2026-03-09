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
        "non-equity incentive plan compensation",
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
    "performance restricted stock unit",
    "performance-based rsu",
    "performance based rsu",
    "prsu",
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
    return re.sub(r"\s+", " ", value.strip().lower())


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
    return any(signature in heading for signature in signatures)


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


def _build_column_map(table_block: TableBlock, schema: dict[str, list[str]]) -> list[str | None]:
    if not table_block.rows:
        return []

    header_rows = table_block.header_row_count if table_block.header_row_count > 0 else 1
    header_rows = min(header_rows, len(table_block.rows))
    header_slice = table_block.rows[:header_rows]
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
        if canonical in seen:
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


def _infer_grants_header_rows(table_block: TableBlock) -> int:
    """Infer grants header depth even when parser reports header_row_count=0."""
    if not table_block.rows:
        return 0

    max_scan = min(6, len(table_block.rows))
    subheader_index: int | None = None
    for index in range(max_scan):
        text = _row_text(table_block.rows[index])
        if not text:
            continue
        if "name" in text and "grant date" in text and ("threshold" in text or "target" in text or "maximum" in text):
            subheader_index = index
            break

    if subheader_index is not None:
        return max(1, subheader_index + 1)

    # Fallback when no clear subheader row is detected.
    if table_block.header_row_count > 0:
        return min(table_block.header_row_count, len(table_block.rows))
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
    resolved_header_rows = min(max(1, resolved_header_rows), len(table_block.rows), 6)
    header_slice = table_block.rows[:resolved_header_rows]
    column_count = max((len(row) for row in header_slice), default=0)

    group_context: list[str | None] = [None] * column_count
    for row in header_slice:
        last_group: str | None = None
        for column_index in range(column_count):
            cell = _normalise(row[column_index]) if column_index < len(row) else ""
            if _contains_any(cell, _GRANTS_NON_EQUITY_HINTS):
                last_group = "non_equity"
                group_context[column_index] = "non_equity"
                continue
            if _contains_any(cell, _GRANTS_EQUITY_HINTS) and "non-equity" not in cell and "non equity" not in cell:
                last_group = "equity"
                group_context[column_index] = "equity"
                continue
            if not cell and last_group is not None and group_context[column_index] is None:
                group_context[column_index] = last_group

    column_map: list[str | None] = []
    seen: set[str] = set()

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
            "fair value" in combined_header and "grant date" in combined_header
        ):
            canonical = "grant_date_fair_value"
        elif "grant date" in combined_header:
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
            if _contains_any(combined_header, _GRANTS_NON_EQUITY_HINTS):
                canonical = "non_equity_threshold"
            elif _contains_any(combined_header, _GRANTS_EQUITY_HINTS):
                canonical = "equity_threshold"
            elif group_context[column_index] == "non_equity":
                canonical = "non_equity_threshold"
            elif group_context[column_index] == "equity":
                canonical = "equity_threshold"
        elif "target" in combined_header:
            if _contains_any(combined_header, _GRANTS_NON_EQUITY_HINTS):
                canonical = "non_equity_target"
            elif _contains_any(combined_header, _GRANTS_EQUITY_HINTS):
                canonical = "equity_target"
            elif group_context[column_index] == "non_equity":
                canonical = "non_equity_target"
            elif group_context[column_index] == "equity":
                canonical = "equity_target"
        elif "maximum" in combined_header or " max " in f" {combined_header} ":
            if _contains_any(combined_header, _GRANTS_NON_EQUITY_HINTS):
                canonical = "non_equity_maximum"
            elif _contains_any(combined_header, _GRANTS_EQUITY_HINTS):
                canonical = "equity_maximum"
            elif group_context[column_index] == "non_equity":
                canonical = "non_equity_maximum"
            elif group_context[column_index] == "equity":
                canonical = "equity_maximum"
        else:
            canonical = _match_col(combined_header, schema)

        if canonical in seen:
            canonical = None
        if canonical is not None:
            seen.add(canonical)
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
        output[canonical] = value
        if canonical in NUMERIC_COLUMNS:
            numeric_val = clean_numeric(value)
            output[canonical] = numeric_val if numeric_val is not None else value
        mapped_values += 1

    if mapped_values == 0 and row:
        output["exec_name"] = row[0].strip()

    # Split "Name and Principal Position" cells into name + title when possible.
    raw_exec = str(output.get("exec_name", "") or "")
    if raw_exec and not output.get("exec_title"):
        if "\n" in raw_exec:
            parts = [part.strip() for part in raw_exec.split("\n", 1) if part.strip()]
            if len(parts) == 2:
                output["exec_name"] = parts[0]
                output["exec_title"] = parts[1]
        elif "," in raw_exec:
            name_part, _, title_part = raw_exec.partition(",")
            title_candidate = title_part.strip()
            title_keywords = {
                "officer",
                "president",
                "director",
                "chairman",
                "executive",
                "ceo",
                "cfo",
                "coo",
                "svp",
                "evp",
                "vp",
            }
            if any(keyword in title_candidate.lower() for keyword in title_keywords):
                output["exec_name"] = name_part.strip()
                output["exec_title"] = title_candidate

    output["footnote_refs"] = _extract_row_footnote_refs(row, footnotes)
    output["source_section"] = source_section
    output["table_block_id"] = table_block_id
    return output


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


def extract_summary_compensation(
    blocks: list[BaseBlock],
    meta: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Extract Summary Compensation Table rows."""
    raw_rows = _extract_table(
        blocks,
        _TABLE_SIGNATURES["summary_compensation"],
        _SUMMARY_COMP_COLS,
        meta,
    )
    if not raw_rows:
        return []

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
        column_map = _build_column_map(table, _SUMMARY_COMP_COLS)
        if not _summary_table_has_comp_columns(column_map):
            continue
        valid_rows.extend(table_rows)

    return valid_rows


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
    return bool(normalized) and any(hint in normalized for hint in _GRANTS_TYPE_ROW_HINTS)


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
        mapped = _map_row(
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
