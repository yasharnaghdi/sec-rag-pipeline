from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from typing import Any

import pytest

from ingestion.metadata_model import (
    DocumentMetadata,
    FootnoteBlock,
    HeadingBlock,
    ImageBlock,
    ProseBlock,
    TableBlock,
    XBRLAnnotation,
    XBRLTaggedBlock,
)


def _base_kwargs() -> dict[str, Any]:
    return {
        "document_id": "doc-connectone-2025",
        "section_id": "section-exec-comp",
        "order_index": 1,
        "source_char_start": 100,
        "source_char_end": 180,
    }


@pytest.mark.parametrize(
    ("instance",),
    [
        (
            ProseBlock(
                **_base_kwargs(),
                text="Compensation committee discussed base salary adjustments.",
                token_count=7,
            ),
        ),
        (
            HeadingBlock(
                **_base_kwargs(),
                text="Executive Compensation",
                level=2,
                detection_method="keyword_match",
            ),
        ),
        (
            TableBlock(
                **_base_kwargs(),
                rows=[["Name", "Salary"], ["Jane Doe", "$1,000,000"]],
                header_row_count=1,
                linearized_text="Name Salary Jane Doe $1,000,000",
                footnotes={},
                has_merged_cells=False,
                token_count_linearized=6,
            ),
        ),
        (
            ImageBlock(
                **_base_kwargs(),
                alt_text="Board matrix",
                position_token="[IMAGE:Board matrix]",
                caption_text="Board tenure and diversity matrix.",
            ),
        ),
        (
            FootnoteBlock(
                **_base_kwargs(),
                marker="(1)",
                text="Amounts reflect annualized salary for part-year service.",
                linked_table_id=None,
            ),
        ),
        (
            XBRLTaggedBlock(
                **_base_kwargs(),
                text="Salary amount 1000000.",
                xbrl_tags=[
                    XBRLAnnotation(
                        concept_name="us-gaap:SalaryAndWagesBenefitsAndExpenses",
                        value="1000000",
                        context_ref="CurrentYearContext",
                    )
                ],
                token_count=3,
            ),
        ),
    ],
)
def test_model_round_trip_json(instance: object) -> None:
    payload = instance.model_dump_json()
    restored = type(instance).model_validate_json(payload)
    assert restored.model_dump() == instance.model_dump()


def test_document_metadata_round_trip() -> None:
    metadata = DocumentMetadata(
        document_id="doc-connectone-2025",
        cik="0000712771",
        company_name="ConnectOne Bancorp, Inc.",
        form_type="DEF 14A",
        filing_date=date(2025, 4, 1),
        accession_number="0000000000-25-000001",
        source_url="https://www.sec.gov/Archives/edgar/data/712771/example.htm",
        fiscal_year_end=date(2024, 12, 31),
        raw_html_path="data/raw/connectone/2025-def14a.html",
    )
    restored = DocumentMetadata.model_validate_json(metadata.model_dump_json())
    assert restored.model_dump() == metadata.model_dump()


def test_id_is_deterministic_across_python_processes() -> None:
    script = """
import json
from ingestion.metadata_model import ProseBlock

block = ProseBlock(
    document_id="doc-connectone-2025",
    section_id="section-exec-comp",
    order_index=1,
    source_char_start=100,
    source_char_end=180,
    text="Compensation committee discussed base salary adjustments.",
    token_count=7,
)
print(json.dumps({"id": block.id}))
"""

    first = subprocess.check_output([sys.executable, "-c", script], text=True)
    second = subprocess.check_output([sys.executable, "-c", script], text=True)

    first_id = json.loads(first)["id"]
    second_id = json.loads(second)["id"]

    assert first_id == second_id
    assert len(first_id) == 64
    assert all(ch in "0123456789abcdef" for ch in first_id)


def test_table_block_empty_footnotes_serializes_to_empty_dict() -> None:
    block = TableBlock(
        **_base_kwargs(),
        rows=[["Name", "Salary"]],
        header_row_count=1,
        linearized_text="Name Salary",
        footnotes={},
        has_merged_cells=False,
        token_count_linearized=2,
    )

    dumped = block.model_dump()
    assert dumped["footnotes"] == {}
    assert '"footnotes":{}' in block.model_dump_json()


def test_xbrl_tagged_block_round_trip_with_annotation() -> None:
    block = XBRLTaggedBlock(
        **_base_kwargs(),
        text="Bonus amount 500000.",
        xbrl_tags=[
            XBRLAnnotation(
                concept_name="us-gaap:Bonus",
                value="500000",
                context_ref="CurrentYearContext",
            )
        ],
        token_count=3,
    )
    restored = XBRLTaggedBlock.model_validate_json(block.model_dump_json())
    assert len(restored.xbrl_tags) == 1
    assert restored.model_dump() == block.model_dump()
