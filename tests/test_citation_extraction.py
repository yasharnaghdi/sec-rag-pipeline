"""TDD Gate — Phase 0 / Phase 4.

LLM responses must contain verbatim-style attribution with:
- CIK reference
- Section name
- Chunk index

This test stubs the LLM call and validates prompt structure.
"""
from __future__ import annotations

import pytest

from core.models import BlockType, Chunk
from generation.prompt_builder import build_prompt
from uuid import uuid4


def _make_chunk(idx: int, text: str) -> Chunk:
    return Chunk(
        source_block_id=uuid4(),
        text=text,
        chunk_type=BlockType.PARAGRAPH,
        token_count=len(text.split()),
        chunk_index=idx,
    )


class TestCitationExtraction:
    def test_prompt_contains_chunk_index_markers(self) -> None:
        """The assembled prompt must include [chunk:N] markers for each chunk."""
        chunks = [
            _make_chunk(0, "Tim Cook received $3M base salary."),
            _make_chunk(1, "The Board approved a retention grant of 963,767 RSUs."),
        ]
        messages = build_prompt("What was Tim Cook's compensation?", chunks)
        context_msg = messages[1]["content"]
        assert "[chunk:0]" in context_msg
        assert "[chunk:1]" in context_msg

    def test_system_prompt_instructs_citation_format(self) -> None:
        """System prompt must instruct the LLM to use CIK/section/chunk citations."""
        chunks = [_make_chunk(0, "Sample text.")]
        messages = build_prompt("Any question", chunks)
        system_content = messages[0]["content"]
        assert "CIK" in system_content
        assert "chunk" in system_content

    def test_prompt_includes_all_chunk_texts(self) -> None:
        chunk_texts = [
            "Executive compensation totalled $12M.",
            "RSU grants vest over four years.",
        ]
        chunks = [_make_chunk(i, t) for i, t in enumerate(chunk_texts)]
        messages = build_prompt("compensation question", chunks)
        context = messages[1]["content"]
        for text in chunk_texts:
            assert text in context, f"Chunk text not found in prompt: {text!r}"
