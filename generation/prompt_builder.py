"""LLM prompt assembly with citation extraction.

TODO (Phase 4):
- Implement build_prompt(query, chunks) with ±2 paragraph context window
- Implement extract_citations(llm_response, chunks) -> list[Citation]
"""
from __future__ import annotations

from core.models import Chunk

_SYSTEM_PROMPT = """You are a financial analyst assistant. Answer questions about SEC filings accurately.
For every claim, cite the specific filing section using the format: [CIK:{cik} | {section} | chunk:{idx}].
If the answer is not in the provided context, say "Not found in the provided filings."
"""


def build_prompt(query: str, chunks: list[Chunk]) -> list[dict[str, str]]:
    """Assemble messages list for OpenAI chat completion."""
    context_parts = []
    for c in chunks:
        context_parts.append(
            f"[chunk:{c.chunk_index}]\n{c.text}\n"
        )
    context = "\n---\n".join(context_parts)

    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {
            "role": "user",
            "content": f"Context:\n{context}\n\nQuestion: {query}",
        },
    ]
