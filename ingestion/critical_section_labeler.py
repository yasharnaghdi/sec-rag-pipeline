"""Rule-based labeling for compensation-critical SEC proxy sections."""
from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ingestion.comp_table_extractor import (
    extract_equity_awards,
    extract_grants_plan_based,
    extract_option_exercises,
    extract_pension_benefits,
    extract_summary_compensation,
)
from ingestion.metadata_model import BaseBlock, FootnoteBlock, HeadingBlock, ProseBlock, TableBlock, XBRLTaggedBlock

SUMMARY_COMP_SIGS = (
    "summary compensation table",
    "summary compensation",
    "named executive officer compensation",
    "compensation of named executive officers",
)
EQUITY_AWARDS_SIGS = (
    "outstanding equity awards",
    "equity awards outstanding",
    "unexercised options",
)
GRANTS_PLAN_BASED_SIGS = (
    "grants of plan-based awards",
    "grants of plan based awards",
    "plan-based award grants",
    "incentive plan awards",
)
OPTION_EXERCISES_SIGS = (
    "option exercises and stock vested",
    "options exercised and stock vested",
    "option exercises and stock awards vested",
)
PENSION_BENEFITS_SIGS = (
    "pension benefits",
    "nonqualified deferred compensation",
    "defined benefit",
    "retirement benefits",
    "serp",
)
CDA_SIGS = (
    "compensation discussion and analysis",
    "cd&a",
    "cda",
)
EXEC_COMP_SIGS = (
    "executive compensation",
    "executive compensation discussion",
)
PAY_VS_PERFORMANCE_SIGS = (
    "pay versus performance",
    "pay vs performance",
    "pay vs. performance",
)

_LOOKBACK_BLOCKS = 12


@dataclass(frozen=True)
class _SectionSpec:
    key: str
    aliases: tuple[str, ...]
    table_scoped: bool = False
    extractor: Callable[[list[BaseBlock], dict[str, Any]], list[dict[str, Any]]] | None = None


_SECTION_SPECS: tuple[_SectionSpec, ...] = (
    _SectionSpec(
        "summary_comp",
        SUMMARY_COMP_SIGS,
        table_scoped=True,
        extractor=extract_summary_compensation,
    ),
    _SectionSpec(
        "equity_awards",
        EQUITY_AWARDS_SIGS,
        table_scoped=True,
        extractor=extract_equity_awards,
    ),
    _SectionSpec(
        "grants_plan_based",
        GRANTS_PLAN_BASED_SIGS,
        table_scoped=True,
        extractor=extract_grants_plan_based,
    ),
    _SectionSpec(
        "option_exercises",
        OPTION_EXERCISES_SIGS,
        table_scoped=True,
        extractor=extract_option_exercises,
    ),
    _SectionSpec(
        "pension_benefits",
        PENSION_BENEFITS_SIGS,
        table_scoped=True,
        extractor=extract_pension_benefits,
    ),
    _SectionSpec("cda", CDA_SIGS),
    _SectionSpec("exec_comp", EXEC_COMP_SIGS),
    _SectionSpec("pay_vs_performance", PAY_VS_PERFORMANCE_SIGS),
)


def _normalise(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def _matches_aliases(text: str, aliases: tuple[str, ...]) -> bool:
    normalized = _normalise(text)
    return any(alias in normalized for alias in aliases)


def _block_text(block: BaseBlock) -> str:
    if isinstance(block, (HeadingBlock, ProseBlock, XBRLTaggedBlock, FootnoteBlock)):
        return block.text
    if isinstance(block, TableBlock):
        return block.linearized_text
    return ""


def _block_token_count(block: BaseBlock) -> int:
    if isinstance(block, TableBlock):
        return block.token_count_linearized
    if isinstance(block, (ProseBlock, XBRLTaggedBlock)):
        return block.token_count
    text = _block_text(block)
    return len(text.split()) if text else 0


def _default_label_output() -> dict[str, Any]:
    output: dict[str, Any] = {}
    for spec in _SECTION_SPECS:
        output[f"has_{spec.key}"] = False
        output[f"{spec.key}_token_count"] = 0
        output[f"{spec.key}_block_ids"] = []
    return output


def label_critical_sections(blocks: list[BaseBlock]) -> dict[str, Any]:
    """Return rule-based section presence flags, token counts, and block ids."""
    output = _default_label_output()
    table_by_id = {
        block.id: block
        for block in blocks
        if isinstance(block, TableBlock)
    }
    heading_by_id = {
        block.id: block
        for block in blocks
        if isinstance(block, HeadingBlock)
    }

    for spec in _SECTION_SPECS:
        matched_heading_ids = {
            heading.id
            for heading in heading_by_id.values()
            if _matches_aliases(heading.text, spec.aliases)
        }
        block_ids: list[str] = []
        token_count = 0

        if spec.table_scoped:
            if spec.extractor is not None:
                try:
                    extracted_rows = spec.extractor(blocks, {})
                except Exception:
                    extracted_rows = []
                for row in extracted_rows:
                    table_id = str(row.get("table_block_id", "") or "").strip()
                    if not table_id or table_id not in table_by_id:
                        continue
                    block_ids.append(table_id)
                deduped_ids = list(dict.fromkeys(block_ids))
                if deduped_ids:
                    token_count = sum(_block_token_count(table_by_id[block_id]) for block_id in deduped_ids)
                    output[f"has_{spec.key}"] = True
                    output[f"{spec.key}_token_count"] = token_count
                    output[f"{spec.key}_block_ids"] = deduped_ids
                    continue

            for index, block in enumerate(blocks):
                if not isinstance(block, TableBlock):
                    continue

                section_heading = heading_by_id.get(block.section_id)
                matched = section_heading is not None and _matches_aliases(section_heading.text, spec.aliases)
                if not matched:
                    start = max(0, index - _LOOKBACK_BLOCKS)
                    for candidate in blocks[start:index]:
                        if isinstance(candidate, HeadingBlock) and _matches_aliases(candidate.text, spec.aliases):
                            matched = True
                            break
                if not matched:
                    continue

                block_ids.append(block.id)
                token_count += _block_token_count(block)
        else:
            for heading_id in matched_heading_ids:
                heading = heading_by_id[heading_id]
                block_ids.append(heading.id)
                token_count += _block_token_count(heading)
                for block in blocks:
                    if block.section_id != heading_id or isinstance(block, HeadingBlock):
                        continue
                    block_ids.append(block.id)
                    token_count += _block_token_count(block)

        deduped_ids = list(dict.fromkeys(block_ids))
        output[f"has_{spec.key}"] = bool(deduped_ids)
        output[f"{spec.key}_token_count"] = token_count
        output[f"{spec.key}_block_ids"] = deduped_ids

    return output
