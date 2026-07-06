"""Application configuration loaded from environment (12-factor)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"


@dataclass(frozen=True)
class Settings:
    openai_api_key: str
    chat_model: str
    router_model: str         # cheap model for query rewrite + rerank
    embed_model: str
    rag_top_k: int
    rag_min_score: float
    rerank_candidates: int    # how many Qdrant hits to fetch before reranking
    qdrant_url: str           # Qdrant server URL (RAG vector store)
    qdrant_api_key: Optional[str]  # required for Qdrant Cloud
    qdrant_collection: str    # collection populated by scripts/embed_to_qdrant.py
    data_dir: Path
    history_turns: int        # how many prior messages (from the client) to feed the LLM
    # Kafka
    kafka_enabled: bool
    kafka_host: str
    kafka_input_topic: str
    kafka_output_topic: str
    kafka_group_id: str
    # WebSocket (SocketCluster)
    ws_enabled: bool
    ws_url: str
    # Langfuse observability
    langfuse_enabled: bool
    langfuse_secret_key: str
    langfuse_public_key: str
    langfuse_host: str
    langfuse_timeout: int   # milliseconds

    @staticmethod
    def load() -> "Settings":
        data_dir = Path(os.getenv("DATA_DIR", str(DATA_DIR)))
        return Settings(
            openai_api_key=os.getenv("OPENAI_API_KEY", ""),  # set in .env — never hardcode
            chat_model=os.getenv("OPENAI_CHAT_MODEL", "gpt-4o"),
            router_model=os.getenv("OPENAI_ROUTER_MODEL", "gpt-4o-mini"),
            embed_model=os.getenv("OPENAI_EMBED_MODEL", "text-embedding-3-small"),
            rag_top_k=int(os.getenv("RAG_TOP_K", "3")),
            rag_min_score=float(os.getenv("RAG_MIN_SCORE", "0.30")),
            rerank_candidates=int(os.getenv("RERANK_CANDIDATES", "8")),
            qdrant_url=os.getenv("QDRANT_URL", "http://localhost:6333"),
            qdrant_api_key=os.getenv("QDRANT_API_KEY") or None,
            qdrant_collection=os.getenv("QDRANT_COLLECTION", "chatbot_sd_kb"),
            data_dir=data_dir,
            history_turns=int(os.getenv("HISTORY_TURNS", "6")),
            kafka_enabled=os.getenv("KAFKA_ENABLED", "false").lower() == "true",
            kafka_host=os.getenv("KAFKA_HOST", "localhost:9092"),
            kafka_input_topic=os.getenv("KAFKA_INPUT_TOPIC", "ai-agent-chat"),
            kafka_output_topic=os.getenv("KAFKA_OUTPUT_TOPIC", "bot-agent-service"),
            kafka_group_id=os.getenv("KAFKA_GROUP_ID", "ai-agent-consumer"),
            ws_enabled=os.getenv("WS_ENABLED", "false").lower() == "true",
            ws_url=os.getenv("WS_URL", "ws://localhost:8001/socketcluster/"),
            langfuse_enabled=os.getenv("LANGFUSE_ENABLED", "false").lower() == "true",
            langfuse_secret_key=os.getenv("LANGFUSE_SECRET_KEY", ""),
            langfuse_public_key=os.getenv("LANGFUSE_PUBLIC_KEY", ""),
            langfuse_host=os.getenv("LANGFUSE_BASE_URL", "https://cloud.langfuse.com"),
            langfuse_timeout=int(os.getenv("LANGFUSE_TIMEOUT", "5000")),
        )