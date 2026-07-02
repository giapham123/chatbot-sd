"""Composition root — the ONE place concrete adapters are wired to abstractions.

Everything above this file depends only on interfaces. Change a backend (OpenAI
-> Azure, CSV -> Postgres, in-memory -> Qdrant) here and nowhere else (DIP).
"""
from __future__ import annotations

from dataclasses import dataclass

from langgraph.checkpoint.memory import MemorySaver
from openai import AsyncOpenAI
from qdrant_client import AsyncQdrantClient

from .services.actions import LoggingActionExecutor
from .config import Settings
from .orchestration.conversation import ConversationService
from .orchestration.graph import build_conversation_graph
from .services.llm import OpenAIEmbeddingClient, OpenAILLMClient
from .services.rag import DefaultRagService
from .repositories.csv_repositories import CsvDataContext
from .services.vector_store import QdrantVectorStore


@dataclass
class Container:
    conversation: ConversationService
    _conns: list  # open async clients to close on shutdown

    async def close(self) -> None:
        for conn in self._conns:
            try:
                await conn.close()
            except Exception:
                pass


async def build_container(settings: Settings) -> Container:
    client = AsyncOpenAI(api_key=settings.openai_api_key)

    # Adapters
    data = CsvDataContext(settings.data_dir)
    llm = OpenAILLMClient(client, settings.chat_model)
    router_llm = OpenAILLMClient(client, settings.router_model)  # cheap: rewrite + rerank
    embedder = OpenAIEmbeddingClient(client, settings.embed_model)
    # Vectors live in Qdrant (populated by scripts/embed_to_qdrant.py) — nothing in RAM.
    qdrant_client = AsyncQdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)
    vector_store = QdrantVectorStore(qdrant_client, settings.qdrant_collection)
    action_executor = LoggingActionExecutor()

    # Graph state (greeted / awaiting_admin) lives in RAM, keyed by session_id.
    # Lost on restart — chat history is kept client-side in localStorage instead.
    checkpointer = MemorySaver()

    # Services — conversational RAG (no scripted flows)
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

    graph = build_conversation_graph(
        rag=rag,
        action_executor=action_executor,
        welcome_text=data.responses.text("welcome"),
        ask_msnv_text=data.responses.text("fb_ask"),
        forwarded_text=data.responses.text("fb_done"),
        notify_action=data.actions.get("notify_admin"),
        checkpointer=checkpointer,
    )
    conversation = ConversationService(graph, rag, settings.history_turns)
    return Container(conversation=conversation, _conns=[qdrant_client])