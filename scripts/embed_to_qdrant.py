"""Embed the knowledge base into Qdrant.

Reads `data/knowledge_base.csv`, creates OpenAI embeddings for each document, and
upserts them into a Qdrant collection (payload = doc metadata + text).

Run a Qdrant server first (local example):
    docker run -p 6333:6333 -p 6334:6334 -v qdrant_storage:/qdrant/storage qdrant/qdrant

Install deps:
    pip install qdrant-client openai python-dotenv

Configure via env / .env:
    OPENAI_API_KEY=sk-...
    OPENAI_EMBED_MODEL=text-embedding-3-small      # optional (default)
    QDRANT_URL=http://localhost:6333               # or your Qdrant Cloud URL
    QDRANT_API_KEY=                                # required for Qdrant Cloud
    QDRANT_COLLECTION=chatbot_sd_kb                # optional (default)

Run:
    python scripts/embed_to_qdrant.py
    python scripts/embed_to_qdrant.py --recreate   # drop & rebuild the collection
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI
from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

# Make `app` importable when running this script directly.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.repositories.csv_repositories import CsvKnowledgeRepository  # noqa: E402

load_dotenv()

EMBED_MODEL = os.getenv("OPENAI_EMBED_MODEL", "text-embedding-3-small")
QDRANT_URL = os.getenv("QDRANT_URL", "http://localhost:6333")
QDRANT_API_KEY = os.getenv("QDRANT_API_KEY") or None
COLLECTION = os.getenv("QDRANT_COLLECTION", "chatbot_sd_kb")
KB_PATH = ROOT / "data" / "knowledge_base.csv"
BATCH = 100


def embed_batches(client: OpenAI, texts: list[str]) -> list[list[float]]:
    """Embed texts in batches; returns one vector per text (order preserved)."""
    vectors: list[list[float]] = []
    for i in range(0, len(texts), BATCH):
        chunk = texts[i:i + BATCH]
        resp = client.embeddings.create(model=EMBED_MODEL, input=chunk)
        vectors.extend(item.embedding for item in resp.data)
        print(f"  embedded {min(i + BATCH, len(texts))}/{len(texts)}")
    return vectors


def main() -> None:
    parser = argparse.ArgumentParser(description="Embed knowledge_base.csv into Qdrant")
    parser.add_argument("--recreate", action="store_true",
                        help="Drop the collection first, then rebuild it")
    args = parser.parse_args()

    if not os.getenv("OPENAI_API_KEY"):
        raise SystemExit("OPENAI_API_KEY is not set (put it in .env).")

    # 1) Load knowledge base rows.
    docs = CsvKnowledgeRepository(KB_PATH).all()
    if not docs:
        raise SystemExit(f"No documents found in {KB_PATH}")
    print(f"Loaded {len(docs)} KB documents from {KB_PATH.name}")

    # 2) Embed every document (question + answer).
    openai_client = OpenAI()
    print(f"Embedding with model '{EMBED_MODEL}'…")
    vectors = embed_batches(openai_client, [d.as_text() for d in docs])
    dim = len(vectors[0])
    print(f"Vector dimension = {dim}")

    # 3) Ensure the collection exists (create / recreate).
    qdrant = QdrantClient(url=QDRANT_URL, api_key=QDRANT_API_KEY)
    exists = qdrant.collection_exists(COLLECTION)
    if exists and args.recreate:
        qdrant.delete_collection(COLLECTION)
        exists = False
    if not exists:
        qdrant.create_collection(
            collection_name=COLLECTION,
            vectors_config=VectorParams(size=dim, distance=Distance.COSINE),
        )
        print(f"Created collection '{COLLECTION}' (size={dim}, cosine)")
    else:
        print(f"Using existing collection '{COLLECTION}' (upserting)")

    # 4) Upsert points. Payload carries the metadata we need at query time.
    points = [
        PointStruct(
            id=i,
            vector=vec,
            payload={
                "doc_id": d.doc_id,
                "topic_id": d.topic_id,
                "question": d.question,
                "answer": d.answer,
                "source": d.source,
                "tags": d.tags,
                "text": d.as_text(),
            },
        )
        for i, (d, vec) in enumerate(zip(docs, vectors))
    ]
    qdrant.upsert(collection_name=COLLECTION, points=points)

    count = qdrant.count(COLLECTION).count
    print(f"✅ Done. Upserted {len(points)} points. Collection now has {count} points.")


if __name__ == "__main__":
    main()