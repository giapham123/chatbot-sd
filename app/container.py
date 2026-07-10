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
from .services.agent_graph import agent_graph
from .services.langfuse_service import langfuse_service
from .services.minio_service import minio_service
from .services.staff_check_service import staff_check_service
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
        langfuse_service.init(
            secret_key=settings.langfuse_secret_key,
            public_key=settings.langfuse_public_key,
            host=settings.langfuse_host,
            timeout=settings.langfuse_timeout,
        )

    if settings.staff_check_enabled and settings.staff_check_url:
        staff_check_service.init(
            url=settings.staff_check_url,
            auth=settings.staff_check_auth,
            timeout=settings.staff_check_timeout,
        )

    agent_graph.build(
        settings.chat_model,
        settings.openai_api_key,
        router_model=settings.router_model,
        reasoning_effort=settings.chat_reasoning_effort,
    )

    minio_service.init(
        endpoint=settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
        public_base_url=settings.minio_public_base_url,
        bucket=settings.minio_bucket,
    )

    from openai import AsyncOpenAI
    client = AsyncOpenAI(api_key=settings.openai_api_key)

    data = CsvDataContext(settings.data_dir)
    router_llm = OpenAILLMClient(client, settings.router_model)  # cheap: rewrite + rerank
    embedder = OpenAIEmbeddingClient(client, settings.embed_model)
    # Vectors live in Qdrant (populated by scripts/embed_to_qdrant.py) — nothing in RAM.
    qdrant_client = AsyncQdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    vector_store = QdrantVectorStore(qdrant_client, settings.qdrant_collection)
    await vector_store.connect()

    rag = DefaultRagService(
        embedder=embedder,
        vector_store=vector_store,
        router_llm=router_llm,
        knowledge=data.knowledge,
        top_k=settings.rag_top_k,
        min_score=settings.rag_min_score,
        fallback_message=data.responses.text("handoff_sd"),
        rerank_candidates=settings.rerank_candidates,
        chat_model=settings.chat_model,
    )

    agent_graph.set_qdrant_search(rag.search_kb)

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