"""Voyage Finance-2 embedding wrapper.

This wrapper keeps the Phase 2 contract intentionally small:
- default model remains ``voyage-finance-2``
- empty batches return immediately
- missing API credentials fail clearly on first real embed request
"""
from __future__ import annotations

from typing import Any

from core.config import get_settings


class VoyageEmbedder:
    """Wraps voyageai client for Finance-2 embeddings."""

    def __init__(self) -> None:
        settings = get_settings()
        self._api_key = settings.voyage_api_key.strip()
        self._model = settings.voyage_model or "voyage-finance-2"
        self._client: Any | None = None

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client

        if not self._api_key:
            msg = "VOYAGE_API_KEY is required to generate embeddings."
            raise ValueError(msg)

        try:
            from voyageai.client import Client
        except ImportError as exc:
            msg = "voyageai package is not installed."
            raise RuntimeError(msg) from exc

        self._client = Client(api_key=self._api_key)
        return self._client

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Return Voyage Finance-2 embeddings in the same order as ``texts``."""
        if not texts:
            return []

        response = self._get_client().embed(
            texts,
            model=self._model,
            input_type="document",
        )
        embeddings = getattr(response, "embeddings", None)
        if not isinstance(embeddings, list):
            msg = "Voyage API returned an unexpected embeddings payload."
            raise TypeError(msg)

        ordered_embeddings: list[list[float]] = []
        for embedding in embeddings:
            if not isinstance(embedding, list):
                msg = "Voyage API returned a non-list embedding vector."
                raise TypeError(msg)
            ordered_embeddings.append([float(value) for value in embedding])

        if len(ordered_embeddings) != len(texts):
            msg = (
                "Voyage API returned "
                f"{len(ordered_embeddings)} embeddings for {len(texts)} input texts."
            )
            raise ValueError(msg)

        return ordered_embeddings
