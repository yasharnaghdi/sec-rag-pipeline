"""Hybrid retrieval: vector similarity + BM25/FTS, merged via Reciprocal Rank Fusion.

TODO (Phase 4):
- Implement vector_search(query_embedding, top_k) via Qdrant
- Implement bm25_search(query_text, top_k) via PostgreSQL GIN tsvector
- Implement rrf_merge(vector_results, bm25_results) -> merged_results
- Implement rerank(query, candidates, top_n) via cross-encoder
"""
from __future__ import annotations

from core.models import Chunk


def reciprocal_rank_fusion(
    results_a: list[Chunk],
    results_b: list[Chunk],
    k: int = 60,
) -> list[tuple[Chunk, float]]:
    """Merge two ranked lists using RRF. k=60 is the standard constant."""
    scores: dict[str, float] = {}
    chunk_map: dict[str, Chunk] = {}

    for rank, chunk in enumerate(results_a, start=1):
        cid = str(chunk.id)
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
        chunk_map[cid] = chunk

    for rank, chunk in enumerate(results_b, start=1):
        cid = str(chunk.id)
        scores[cid] = scores.get(cid, 0.0) + 1.0 / (k + rank)
        chunk_map[cid] = chunk

    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return [(chunk_map[cid], score) for cid, score in ranked]
