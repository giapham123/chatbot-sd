"""Qdrant-backed vector store (implements interfaces.VectorStore).

Queries a Qdrant collection that was populated offline by
`scripts/embed_to_qdrant.py`. No vectors are kept in RAM — every search hits
Qdrant directly, so the KB can be rebuilt/updated without restarting the app.

The collection stores `doc_id` in each point's payload; `search` returns those
doc_ids so the RAG service can map hits back to KBDoc objects.
"""
from __future__ import annotations

import logging

from qdrant_client import AsyncQdrantClient

logger = logging.getLogger(__name__)


class QdrantVectorStore:
    def __init__(self, client: AsyncQdrantClient, collection: str) -> None:
        self._client = client
        self._collection = collection

    async def search(self, vector: list[float], top_k: int) -> list[tuple[str, float]]:
        resp = await self._client.query_points(
            collection_name=self._collection,
            query=vector,
            limit=top_k,
            with_payload=True,
        )

        # Print what Qdrant returned for this query.
        logger.info("Qdrant '%s' → %d hit(s):", self._collection, len(resp.points))
        for rank, point in enumerate(resp.points, 1):
            payload = point.payload or {}
            logger.info(
                "  #%d score=%.4f doc_id=%s | Q: %s",
                rank,
                point.score,
                payload.get("doc_id", ""),
                payload.get("question", ""),
            )

        return [
            (point.payload.get("doc_id", ""), point.score)
            for point in resp.points
            if point.payload
        ]