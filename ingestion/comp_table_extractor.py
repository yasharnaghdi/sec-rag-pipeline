"""Extract structured compensation tables from parsed SEC proxy blocks."""
from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

from ingestion.metadata_model import BaseBlock, FootnoteBlock, HeadingBlock, TableBlock

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

_NUMERIC_COLUMNS = {
    "salary",
    "bonus",
    "stock_awards",
    "option_awards",
    "total",
    "grant_fair_value",
    "threshold",
    "target",
    "maximum",
    "options_value",
    "stock_vested_value",
    "present_value",
    "payments",
}

_SUMMARY_COMP_COLS: dict[str, list[str]] = {
    "exec_name": ["name and principal position", "name", "executive"],
    "year": ["year", "fiscal year"],
    "salary": ["salary", "base salary"],
    "bonus": ["bonus", "cash bonus"],
    "stock_awards": ["stock awards", "stock award", "restricted stock", "rsu", "dsu"],
    "option_awards": ["option awards", "option award", "options"],
    "non_equity_incentive": ["non-equity incentive", "non equity incentive", "annual incentive"],
    "pension_change": ["change in pension", "pension value", "nonqualified deferred"],
    "other_comp": ["all other compensation", "all other comp", "other compensation"],
    "total": ["total"],
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
    "threshold": ["threshold"],
    "target": ["target"],
    "maximum": ["maximum", "max"],
    "shares_granted": ["shares", "units", "number of shares"],
    "grant_fair_value": ["grant date fair value", "fair value"],
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
    if not val or val.strip() in ("", "—", "-", "N/A", "n/a"):
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


def _data_rows(table_block: TableBlock) -> list[list[str]]:
    if not table_block.rows:
        return []
    start = table_block.header_row_count if table_block.header_row_count > 0 else 1
    return table_block.rows[min(start, len(table_block.rows)) :]


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
        if canonical in _NUMERIC_COLUMNS:
            output[canonical] = clean_numeric(value)
        else:
            output[canonical] = value
        mapped_values += 1

    if mapped_values == 0 and row:
        output["exec_name"] = row[0].strip()

    output["footnote_refs"] = _extract_row_footnote_refs(row, footnotes)
    output["source_section"] = source_section
    output["table_block_id"] = table_block_id
    return output


def _extract_table(
    blocks: list[BaseBlock],
    signatures: list[str],
    col_schema: dict[str, list[str]],
    meta: Mapping[str, Any],
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

        column_map = _build_column_map(block, col_schema)
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
    return _extract_table(
        blocks,
        _TABLE_SIGNATURES["summary_compensation"],
        _SUMMARY_COMP_COLS,
        meta,
    )


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


def extract_grants_plan_based(
    blocks: list[BaseBlock],
    meta: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Extract Grants of Plan-Based Awards table rows."""
    return _extract_table(
        blocks,
        _TABLE_SIGNATURES["grants_plan_based"],
        _GRANTS_COLS,
        meta,
    )


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
