"""Voyage Finance-2 embedding wrapper.

TODO (Phase 2):
- Implement batch_embed(texts) with Voyage Finance-2
- Implement upsert_to_qdrant(chunks, embeddings)
"""
from __future__ import annotations

from core.config import get_settings


class VoyageEmbedder:
    """Wraps voyageai client for Finance-2 embeddings."""

    def __init__(self) -> None:
        settings = get_settings()
        self._model = settings.voyage_model
        # TODO: self._client = voyageai.Client(api_key=settings.voyage_api_key)

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return embeddings for a batch of texts."""
        raise NotImplementedError("Phase 2: implement Voyage Finance-2 batch embedding")
