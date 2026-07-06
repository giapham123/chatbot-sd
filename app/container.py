"""Composition root — the ONE place concrete adapters are wired to abstractions.

Everything above this file depends only on interfaces. Change a backend (OpenAI
-> Azure, CSV -> Postgres, in-memory -> Qdrant) here and nowhere else (DIP).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

import logging

from qdrant_client import AsyncQdrantClient

from .config import Settings
from .kafka.consumer import KafkaConsumerService
from .kafka.producer import KafkaProducerService
from .orchestration.conversation import ConversationService
from .ws.ws_service import WebsocketClient
from .services.llm import OpenAIEmbeddingClient, OpenAILLMClient
from .services.rag import DefaultRagService
from .services.langfuse_service import langfuse_service
from .repositories.csv_repositories import CsvDataContext
from .services.vector_store import QdrantVectorStore

logger = logging.getLogger(__name__)


@dataclass
class Container:
    conversation: ConversationService
    kafka_consumer: Optional[KafkaConsumerService]
    kafka_producer: Optional[KafkaProducerService]
    ws_client: Optional[WebsocketClient]
    _conns: list = field(default_factory=list)

    async def close(self) -> None:
        for conn in self._conns:
            try:
                await conn.close()
            except Exception:
                pass


async def build_container(settings: Settings) -> Container:
    if settings.langfuse_enabled and settings.langfuse_secret_key and settings.langfuse_public_key:
        import os
        # Set env vars so langfuse.openai wrapper and decorators can pick them up
        os.environ["LANGFUSE_SECRET_KEY"] = settings.langfuse_secret_key
        os.environ["LANGFUSE_PUBLIC_KEY"] = settings.langfuse_public_key
        os.environ["LANGFUSE_HOST"] = settings.langfuse_host
        os.environ["LANGFUSE_TIMEOUT"] = str(settings.langfuse_timeout)
        # Init singleton service with explicit params (mirrors ai-agent pattern)
        langfuse_service.init(
            secret_key=settings.langfuse_secret_key,
            public_key=settings.langfuse_public_key,
            host=settings.langfuse_host,
            timeout=settings.langfuse_timeout,
        )
        from langfuse.openai import AsyncOpenAI
    else:
        from openai import AsyncOpenAI

    client = AsyncOpenAI(api_key=settings.openai_api_key)

    # Adapters
    data = CsvDataContext(settings.data_dir)
    llm = OpenAILLMClient(client, settings.chat_model)
    router_llm = OpenAILLMClient(client, settings.router_model)  # cheap: rewrite + rerank
    embedder = OpenAIEmbeddingClient(client, settings.embed_model)
    # Vectors live in Qdrant (populated by scripts/embed_to_qdrant.py) — nothing in RAM.
    qdrant_client = AsyncQdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    vector_store = QdrantVectorStore(qdrant_client, settings.qdrant_collection)
    await vector_store.connect()

    rag = DefaultRagService(
        embedder=embedder,
        vector_store=vector_store,
        llm=llm,
        router_llm=router_llm,
        knowledge=data.knowledge,
        top_k=settings.rag_top_k,
        min_score=settings.rag_min_score,
        fallback_message=data.responses.text("handoff_sd"),
        rerank_candidates=settings.rerank_candidates,
    )

    conversation = ConversationService(rag, settings.history_turns)

    kafka_consumer: Optional[KafkaConsumerService] = None
    kafka_producer: Optional[KafkaProducerService] = None
    if settings.kafka_enabled:
        kafka_consumer = KafkaConsumerService(
            host=settings.kafka_host,
            group_id=settings.kafka_group_id,
            topics=[settings.kafka_input_topic],
        )
        kafka_producer = KafkaProducerService(host=settings.kafka_host)

    ws_client: Optional[WebsocketClient] = None
    if settings.ws_enabled:
        ws_client = WebsocketClient(settings.ws_url)

    return Container(
        conversation=conversation,
        kafka_consumer=kafka_consumer,
        kafka_producer=kafka_producer,
        ws_client=ws_client,
        _conns=[qdrant_client],
    )